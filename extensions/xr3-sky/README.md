# xr3-sky — run committed r3 jobs on the lab's SkyPilot clusters

`xr3-sky` launches committed r3 jobs as **SkyPilot managed jobs** on the lab's Kubernetes
clusters (`at`, `morpho`, `DO`), and brings their outputs back. It is the SkyPilot sibling
of [`xr3-slurm`](../xr3-slurm/README.md): the same job selection, the same `output/done`
idempotency, and the job's house `run.sh` runs unchanged.

The r3 repository stays on your laptop. Pods are disposable, so **Ceph S3 carries
everything** between the two:

- `launch` pushes the job, its dependencies and its git dependencies to S3, then submits;
- the pod rebuilds a small r3 repo from S3, runs `run.sh`, and syncs `output/` back to S3
  every few minutes and once more at exit (on failure too);
- `pull` downloads the outputs into `$R3_REPOSITORY/jobs/<id>/output/`.

The CLI is one script (`xr3-sky`) plus two unit-tested modules: `xr3skylib.py` (pure
helpers: naming, S3 layout, done state, `sky.yaml` validation, task rendering) and
`xr3skys3.py` (the object store and push planning). `xr3sky_bootstrap.sh` is the pod side,
inlined into every task as its `run:`. Design: [`docs/specs/2026-11-23-xr3-sky-design.md`](../../docs/specs/2026-11-23-xr3-sky-design.md).

> **Assumptions.** `xr3-sky` needs committed jobs with the house `run.sh` and the
> `output/done` marker, an environment that comes from a uv lockfile, and S3 access from
> the laptop. The full list, with *what breaks if you don't*, is in
> **[../CONTRACT.md](../CONTRACT.md#for-xr3-sky-skypilot-managed-jobs--lab-kubernetes)**.
> For cluster-level questions (which GPU, priorities, what is free, `sky-up`, access) see
> the lab's `enigma-skypilot` skill and docs.

## Contents

- [Setup (once per laptop)](#setup-once-per-laptop)
- [Worked example](#worked-example)
- [Commands](#commands)
- [`sky.yaml` — resources, versioned with the job](#skyyaml--resources-versioned-with-the-job)
- [Done state, restarts, failures](#done-state-restarts-failures)
- [What happens in the pod](#what-happens-in-the-pod)
- [S3 layout](#s3-layout)
- [Secrets](#secrets)
- [Large outputs and `REMOTE.yaml`](#large-outputs-and-remoteyaml)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

## Setup (once per laptop)

1. **Install.** The r3-tooling installer (`install.sh`, or the saved `update.sh`) puts
   `boto3` into the toolchain venv and writes an `xr3-sky` wrapper next to `xr3`. Check
   with `xr3-sky --help`.
2. **SkyPilot CLI and API.** The `sky` CLI must be on `PATH` (or point `XR3SKY_SKY` at
   it). The API server is reached through Teleport: run **`sky-up`** in each session
   before `launch`, `status`, `logs` or `cancel`. (Setting up `sky` and `sky-up` is
   covered by the lab's SkyPilot docs / the `enigma-skypilot` skill.)
3. **AWS profile `enigma`** for the Ceph RGW. Put your personal portal key in
   `~/.aws/credentials`, and the endpoint in `~/.aws/config`, with **path-style
   addressing** (the RGW does not serve virtual-host bucket names):

   ```ini
   # ~/.aws/credentials
   [enigma]
   aws_access_key_id = ...
   aws_secret_access_key = ...

   # ~/.aws/config
   [profile enigma]
   region = stanford
   endpoint_url = https://rgw-cold.enigmatic.stanford.edu
   s3 =
       addressing_style = path
   ```

   `xr3-sky` itself always uses path-style and the configured `endpoint`; the profile
   settings matter for your own `aws --profile enigma s3 …` commands.
4. **The Stanford VPN.** The RGW is reachable from the laptop **only on the VPN**. The VPN
   and the Teleport tunnel can be up at the same time, so `launch` is one command.
   `logs` and `cancel` need only Teleport; `status` works without the VPN (degraded).
5. **Config.** `xr3-sky` reads the `sky:` section of `~/.config/xr3.yaml` (`$XR3_CONFIG`
   overrides). Every key has a built-in default, so you only write the ones you change.
   You **must** provide a user name, either `sky.user` or `$SKYPILOT_USER` (its local
   part before `@` is used), because SkyPilot names must contain it.

   ```yaml
   sky:
     bucket: federico-r3            # your bucket on the RGW
     prefix: r3                     # key prefix inside the bucket
     endpoint: https://rgw-cold.enigmatic.stanford.edu
     endpoint_internal: null        # opt-in in-cluster endpoint; only if verified to serve the bucket
     aws_profile: enigma
     user: null                     # null => local part of $SKYPILOT_USER
     sync_interval: 300             # seconds between output syncs in the pod
     pull_max_size: 100MB           # pull leaves larger files in S3 (see REMOTE.yaml)
     push_confirm_above: 5GB        # launch asks before pushing more than this
     secrets: [WANDB_API_KEY]       # secret NAMES forwarded to every job; values from your env
     defaults:                      # SkyPilot task fragment under every job's sky.yaml
       resources: {cpus: 4, memory: 16}
   ```

   Sizes are decimal (`KB`/`MB`/`GB`/`TB`, or plain bytes). The values shown are the
   defaults; the bucket is the author's, so set your own. Copy from
   [`../xr3/xr3.example.yaml`](../xr3/xr3.example.yaml).
6. **`R3_REPOSITORY`** must be set (the installer does it in `~/.bashrc`).

## Worked example

Starting from [`examples/uv-job/`](../../examples/uv-job/) (a CPU job whose environment
is a `uv.lock`, with a `sky.yaml` asking for 1 CPU and 2 GB):

```bash
cp -r ~/r3-toolchain/r3-tooling/examples/uv-job ~/r3_projects/demo/hello
cd ~/r3_projects/demo/hello
# edit metadata.yaml: path demo/hello, tags[0] demo/hello/v1.0.0 (see RESEARCH_WORKFLOW.md)
bash run_inner.sh && rm -rf output .venv    # optional: it runs locally too
xr3 check . && xr3 commit .
ID=$(xr3 history --latest --id .)

sky-up                                      # Teleport tunnel to the SkyPilot API
# connect the Stanford VPN                  # S3 from the laptop
xr3-sky launch --dry "$ID"                  # rendered task.yaml + upload plan; writes nothing
xr3-sky launch "$ID"                        # push to S3, submit; returns once submitted
xr3-sky status 'demo/*'                     # done state + managed-job state
xr3-sky logs "$ID"                          # stream the pod's log
xr3-sky pull "$ID"                          # once it is done: S3 -> $R3_REPOSITORY/jobs/$ID/output
cat "$R3_REPOSITORY/jobs/$ID/output/result.txt"
```

Re-running `xr3-sky launch "$ID"` afterwards prints `<id>: skipping (completed)`.

## Commands

Run `xr3-sky <command> --help` for the flags as the CLI has them.

### `launch [JOB_IDS]… [flags]`

Launches each selected job as the managed job **`<user>-r3-<first 8 chars of the id>`**.
Jobs are selected by `JOB_IDS` and/or `--tag`/`--query`, de-duplicated, exactly as in
`xr3-slurm submit`. Per job, `launch`:

1. checks the done state and skips or proceeds ([table below](#done-state-restarts-failures));
2. merges the job's `sky.yaml` over `sky.defaults` and validates it;
3. plans the push: the job's files, then each transitive job dependency's files and
   output, then a git bundle per pinned git-dependency commit; anything already complete
   in S3 is skipped;
4. renders the SkyPilot task (`setup` installs uv, r3 at your laptop's r3 commit, and the
   AWS CLI; `run` is the bootstrap script);
5. refuses if a managed job with the same name is still queued or running;
6. asks before pushing more than `push_confirm_above`, uploads, writes the markers and a
   copy of the rendered task to `runs/<id>/<ts>/task.yaml`;
7. runs `sky jobs launch -y -d -n <name> --secret … <task.yaml>`. `-d` (detach) returns
   as soon as the job is submitted, so a multi-job launch does not block on each run;
   `launch` then prints `submitted <name>; follow with: xr3-sky logs <id>`.

Steps 1–5 are read-only: a refused spec or push leaves S3 untouched.

| Flag | Effect |
|---|---|
| `-t, --tag TAG` | Select jobs having all these tags (repeatable). |
| `-q, --query JSON` | Select jobs by a JSON query fragment (repeatable; bare `key: value` is wrapped in `{}`). |
| `--dry` | Print the upload plan and the rendered `task.yaml`; upload and submit nothing. Needs S3 (VPN) but not the SkyPilot API. |
| `--infra INFRA` | Narrow placement, e.g. `k8s/at` for a floating CPU job. Refused if `sky.yaml` already sets a different `resources.infra`. |
| `--whole-card` | Fill a missing `nvidia.com/gpumem` cap with the whole-card value. |
| `--allow-high` | Allow `priorityClassName: high` (ask colleagues first). |
| `-r, --restart-failed` | Relaunch jobs whose done state is `failed` (rewrites the marker to `restart`). |
| `--fresh` | Delete the job's S3 `output/` and `output.complete` first, then launch, whatever the done state. |
| `--allow-unfinished-deps` | Push dependencies that have no `output/done` (with a warning). |
| `--yes` | Do not ask before a push larger than `push_confirm_above`. |
| `--force-duplicate` | Launch even if a managed job with the same name is queued or running. |
| `-v, --verbose` | `-v` prints the upload plan; `-vv` also lists every file. |

### `pull [JOB_IDS]… [--max-size SIZE | --all]`

Downloads `jobs/<id>/output/` from S3 into `$R3_REPOSITORY/jobs/<id>/output/`. It skips
files that are already identical (size, and the MD5 where the etag is one), writes the
`done` marker last, and makes `output/` writable as needed. It skips a job with no S3
output (`no S3 output for <id>`) or one still running (`<id>: in progress (no done marker
in S3), skipping`). A `failed` job is pulled like any other, so you can read its logs and
partial output.

| Flag | Effect |
|---|---|
| `-t, --tag` / `-q, --query` | Select jobs, as in `launch`. |
| `--max-size SIZE` | Leave files larger than this in S3 and list them in `output/REMOTE.yaml` (default `sky.pull_max_size`, 100MB). |
| `--all` | Download everything, ignoring `--max-size`. |

Each job ends with a summary line: `<id>: downloaded N, skipped M up-to-date, K remote (size)`.

### `status [GLOB]…`

For jobs whose tags match all the GLOBs (r3 `$glob`, e.g. `'demo/*'`), prints one row per
job: `r3 id`, `tag` (`tags[0]`), `done` (the S3 marker if it exists, else the local one;
`-` for none), `managed` (the newest managed job's SkyPilot state) and `managed id`.
Without the VPN it prints `(S3 unreachable — done column from local markers only)` and
carries on; without `sky-up` it prints `(SkyPilot state unavailable: …)` and shows `?`.

### `logs JOB_ID [--controller]` / `cancel JOB_ID`

Resolve the r3 id to the newest managed job named `<user>-r3-<id[:8]>`, then run
`sky jobs logs [--controller] <managed id>` (streaming) or `sky jobs cancel -y <managed id>`.
They need `sky-up`, not the VPN. `--controller` shows the jobs controller's log (useful
when the job never started).

## `sky.yaml` — resources, versioned with the job

A job may contain a `sky.yaml`: a **fragment of a SkyPilot task** merged (deep, job wins)
over `sky.defaults`. Because it is a committed file, the resources a result was produced
with are part of the job. Allowed top-level keys: `resources`, `config`, `envs`,
`secrets` (names only), `num_nodes` (must be 1). `xr3-sky` owns `name`, `setup`, `run`
and the remaining task keys; `volumes` and `file_mounts` are not available, since all
data moves through S3.

```yaml
# CPU job (floats between clusters unless you pass --infra)
resources: {cpus: 2, memory: 8}
```

```yaml
# GPU job on at's L40S, two HAMi slots
resources:
  accelerators: L40S:1
  cpus: 4
  memory: 32
config:                          # top-level, never inside resources
  kubernetes:
    pod_config:
      spec:
        priorityClassName: medium        # low | medium | high (high needs --allow-high)
        containers:
          - resources:
              limits:
                nvidia.com/gpumem: 9212  # MiB
```

**Rules, checked before anything is written:**

- **A GPU job pins exactly one accelerator** (`accelerators: NAME[:count]`). Each
  accelerator type lives on one cluster, so this pins the cluster too. A list, or a GPU
  inside `any_of`/`ordered`, is refused. CPU-only jobs may use `any_of` to float.
- **HAMi-shared cards need a `nvidia.com/gpumem` cap** (MiB). An uncapped job books the
  whole card while looking like one slot to everyone else. `--whole-card` fills in the
  whole-card value instead. The known cards:

  | Accelerator | Cluster | One slot | Whole card |
  |---|---|---|---|
  | `RTXPRO6000` | morpho | 9788 | 97887 |
  | `H100-80GB` | DO | 8155 | 81559 |
  | `L40S` | at | 4606 | 46068 |

  A cap above the whole-card value is refused. Other accelerators are not HAMi-shared
  and need no cap.
- **Priority** is one of `low`/`medium`/`high`; `high` preempts colleagues, so it needs
  `--allow-high` and a word with them first.
- `config:` must be top-level, `num_nodes` must be 1.

Which GPU to pick, how much VRAM a slot is, and what is free right now are cluster
questions: see the `enigma-skypilot` skill.

## Done state, restarts, failures

**S3 is authoritative.** Once a job has been launched with `xr3-sky`, its `output/done`
in S3 decides; the local copy is only consulted when S3 has none. Marker bodies are
classified like `xr3-slurm`: `restart`, `failed`, and anything else (including an empty
file) is `completed`.

| Done state | `launch` | `launch -r` | `launch --fresh` |
|---|---|---|---|
| none | launch | launch | launch |
| `restart` | launch | launch | launch |
| `failed` | skip | write `restart` to the S3 marker (and the local one, if present), then launch | launch |
| `completed` | skip | skip | launch |

- **A relaunch keeps partial output.** The pod first restores `output/` from S3, so a
  job that checkpoints resumes (RESEARCH_WORKFLOW §10). `--fresh` is the clean slate: it
  deletes the S3 `output/` and `output.complete`, and renames a local `output/done` to
  `output/done.pre-fresh` (other local files stay until the next `pull` overwrites them).
- **Who writes `failed`.** If `run.sh` (or the bootstrap after the restore) exits
  non-zero and no marker exists, the pod writes `failed` before the final sync. A
  termination signal (preemption, `cancel`) never marks `failed`: SkyPilot recovers a
  preempted job, and the new pod resumes from the last sync.
  A failure in SkyPilot's `setup:` phase (installing uv, r3 or the AWS CLI) happens
  before the bootstrap runs, so it marks nothing: the managed job shows `FAILED_SETUP`,
  the done state is unchanged, and a plain `launch` retries. Read
  `xr3-sky logs <id> --controller` to see why.
- **`output.complete`** is written only after a `completed` job's output is fully in S3.
  Downstream launches rely on it to skip re-pushing that output.

## What happens in the pod

`setup` installs `uv` if missing, then r3 (pinned to your laptop's r3 commit, from
`github.com/mtangemann/r3`) and `awscli` as uv tools. `run` is
[`xr3sky_bootstrap.sh`](xr3sky_bootstrap.sh):

1. pick the S3 endpoint: the public `endpoint`, unless `endpoint_internal` is set,
   resolves, and passes an `aws s3 ls` probe of the bucket (it is opt-in and unset by
   default: `rgw-cold.rook-ceph.svc` resolves on at but does not serve the bucket);
2. restore `jobs/<id>/output/` from S3 (resume);
3. build a sparse r3 repo at `$HOME/r3_repo`: `r3.yaml`, the job, every job dependency,
   the git bundles; then `r3 rebuild-index`;
4. sync `output/` to S3 every `sync_interval` seconds in the background;
5. `cd jobs/<id> && R3_REPOSITORY=$HOME/r3_repo bash run.sh`: the house `run.sh` checks
   the job out to a temp dir and runs `run_inner.sh`, which does `uv sync --frozen` /
   `uv run`;
6. on any exit: stop the sync loop, mark `failed` if appropriate, do a final sync (3
   tries), upload `done` last, then `output.complete` for a `completed` job. The pod exits
   with `run.sh`'s status.

Pod log lines are prefixed `[xr3sky]`; read them with `xr3-sky logs <id>`.

## S3 layout

```
s3://<bucket>/<prefix>/                           # default federico-r3 / r3
  jobs/<job-id>/files/…                           # committed job files (no output/), uploaded once
  jobs/<job-id>/files.complete                    # marker: files/ upload is complete
  jobs/<job-id>/output/…                          # job output: synced from the pod, or pushed for deps
  jobs/<job-id>/output/done                       # the done marker (uploaded last)
  jobs/<job-id>/output.complete                   # marker: output/ is complete and final
  git/<sha256(repository_path)>/<commit>.bundle   # a git dependency at one commit
  runs/<job-id>/<launch-ts>/task.yaml             # the exact rendered task of each launch
```

Committed jobs are immutable, so anything behind a `*.complete` marker is never uploaded
again. `runs/…/task.yaml` records where and with what each launch ran; the committed job
itself is never changed. To inspect by hand: `aws --profile enigma s3 ls s3://<bucket>/<prefix>/jobs/<id>/`.

## Secrets

Secret **values never enter job files, S3, the r3 repo or the rendered task.** The task
lists only secret *names*, and `sky jobs launch --secret NAME` reads each value from the
environment of the `xr3-sky` process.

- `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are always forwarded (the pod needs them for
  S3). If they are not in your environment, `xr3-sky` reads them from `sky.aws_profile`.
- `sky.secrets` (default `[WANDB_API_KEY]`) are forwarded when set in your environment.
  A missing one prints `WARNING: secret NAME (sky.secrets) is not set in the environment;
  launching without it` and the job launches without it.
- A job's `sky.yaml` `secrets:` lists names the job **requires**; a missing one is a hard
  error before anything is written. Write them as names only, e.g.
  `secrets: {SPIRAL_TOKEN: null}`.
- Put nothing secret in `envs:`: those values are written into the task and into S3.
- The pod's bootstrap never traces commands (`set -x`), because its environment carries
  the credentials. Keep it that way in your `run_inner.sh`.

## Large outputs and `REMOTE.yaml`

`pull` downloads files up to `--max-size` (default 100MB). Larger files stay in S3 and are
listed in `output/REMOTE.yaml`, one entry per file:

```yaml
- path: checkpoints/model.pt      # relative to output/
  size: 2147483648
  etag: 9b2cf535f27731c974343645a3985328-129
  uri: s3://federico-r3/r3/jobs/<id>/output/checkpoints/model.pt
```

`REMOTE.yaml` is rewritten on every pull, and removed when nothing is left remote
(`pull --all`).

- **As a dependency of another `xr3-sky` job:** a local `REMOTE.yaml` means the full
  output is already in S3, so `launch` pushes nothing for it and the pod gets the
  complete output.
- **As a dependency of a job you run elsewhere** (laptop, SLURM): the large files are
  *not* there. Either `xr3-sky pull --all <id>` first, or have the consumer read
  `REMOTE.yaml` and fetch the `uri`s itself.

## Troubleshooting

Messages are quoted as the CLI prints them (`Error: …` is click's prefix). `<id>` is the
r3 job id.

**Setup and preflight**

| Message | Fix |
|---|---|
| `R3_REPOSITORY is not set` | `export R3_REPOSITORY=~/r3_repo` (or `source ~/.bashrc`). |
| `S3 unreachable — connect to the Stanford VPN` | Connect the VPN. The RGW is not reachable from the laptop otherwise. |
| `SkyPilot API unreachable — run sky-up` | Run `sky-up` (Teleport tunnel to the API server). |
| `` `sky` not found on PATH `` | Install the SkyPilot CLI, or set `XR3SKY_SKY=/path/to/sky`. |
| `` `sky jobs queue` timed out — run sky-up `` | The tunnel is down or stale: rerun `sky-up`. |
| `` `sky jobs queue` failed (exit N): … `` / `cannot run `sky jobs queue`: …` / `cannot parse `sky jobs queue -o json` output: …` / `` `sky jobs queue -o json` did not return a list `` | A problem in the `sky` CLI or API itself; run `sky jobs queue` by hand to see it. |
| `set sky.user or SKYPILOT_USER` | Set `sky.user` in `~/.config/xr3.yaml`, or export `SKYPILOT_USER`. |
| `boto3 is not installed in this environment — re-run the r3-tooling installer` | Re-run the installer (`update.sh`), or `uv pip install --python ~/r3-toolchain/.venv/bin/python boto3`. |
| `AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY not set and boto3 is not installed` | As above. |
| `no AWS credentials in profile 'enigma'` / `AWS profile 'enigma': …` | Add the `[enigma]` key to `~/.aws/credentials` (see Setup). |
| `S3 credentials missing or invalid for profile 'enigma' — see the README's setup section` | The profile is missing, has no key, or the RGW rejected it (403). Fix `~/.aws/credentials` / `~/.aws/config` (see Setup), or `sky.aws_profile`. |
| `bucket 'federico-r3' not found on https://rgw-cold.enigmatic.stanford.edu` | `sky.bucket` (or `sky.endpoint`) is wrong: set it to your own bucket. |
| `cannot determine the r3 commit in <dir>: …` | r3 must be a git checkout (the installer's editable install is). |

**Selecting jobs**

| Message | Fix |
|---|---|
| `No jobs selected (give JOB_IDS, --tag, or --query).` | Pass ids or a selector; check your tag with `xr3 find`. |
| `No run script in <dir>.` | Only jobs with a `run.sh` can be launched (pointer and raw-data jobs cannot). Use the house `run.sh`. |
| `Job directory is not a valid UUID: <dir>` | Pass a committed job id, not a working directory. |
| `job <id> not in local repository` (`pull`) | Wrong id, or a different `R3_REPOSITORY`. |
| `<id>: skipping (completed)` / `(failed)` | Working as intended. `-r` retries `failed`; `--fresh` reruns anything. |
| `<id>: already queued/running as managed job N (STATE); pass --force-duplicate to launch anyway` | Wait, `xr3-sky cancel <id>`, or pass `--force-duplicate` if you really want two. |

**`sky.yaml`** (all prefixed `<id>: `)

| Message | Fix |
|---|---|
| `sky.yaml may only set ['config', 'envs', 'num_nodes', 'resources', 'secrets']; not allowed: [...]` | Remove the other keys; data moves through S3, not `volumes`/`file_mounts`. |
| ``resources.config: must be top-level (use `config:`)`` | Move `config:` out of `resources:`. |
| `num_nodes must be 1` | Multi-node jobs are not supported. |
| `GPU jobs must pin exactly one accelerator` | Use one `accelerators: NAME:count`, not a list or `any_of`. |
| `NAME needs a nvidia.com/gpumem cap: one slot is S MiB, whole card is C MiB (pass --whole-card for the whole card)` | Add `config.kubernetes.pod_config.spec.containers[0].resources.limits["nvidia.com/gpumem"]`, or pass `--whole-card`. |
| `gpumem M exceeds the whole-card value C MiB for NAME` | Lower the cap to at most the whole card. |
| `priority class 'X' not allowed; use one of ['low', 'medium', 'high']` | Fix the `priorityClassName`. |
| `priority class 'high' requires --allow-high` | Ask colleagues first, then pass `--allow-high`. |
| `DO has no persistent volume: MOUNT uses 'do-…'` | Remove the volume from `sky.defaults`; DO has none. |
| `--infra contradicts sky.yaml: 'A' vs 'B'` | `--infra` can only narrow; drop it or change `sky.yaml` (a new commit). |

**Push** (prefixed `<id>: ` where noted)

| Message | Fix |
|---|---|
| `<id>: depends on a container/venv environment job <dep>` | xr3-sky jobs take their environment from `pyproject.toml` + `uv.lock` (see `examples/uv-job/`), not from a container or venv job. |
| `<id>: dependency <dep> has no output locally or in S3` | Run (or `pull`) the dependency first. |
| `<id>: dependency <dep> is unfinished (no output/done); pass --allow-unfinished-deps to push it anyway` | Finish the dependency, or pass the flag on purpose (it then prints `WARNING: dependency <dep> is unfinished (no output/done)`). |
| `<id>: broken symlink: <path>` / `<id>: symlinked directory not supported: <path>` | Fix or remove the link in the job or dependency output; file symlinks are uploaded as their target. |
| `dependency <dep> of <id> not in the repository` | The local repo is missing a job: check `R3_REPOSITORY`. |
| `unresolved git dependency <repo> in <id>` / `unresolved dependency … in <id>` | Only committed jobs (resolved dependencies) can be launched. |
| `commit <sha> not found in <repo dir>: …` / `git bundle create failed in <repo dir>: …` | The local git mirror lacks the pinned commit; `r3 commit` normally fetches it, so re-check the job. |
| `Aborted!` | You declined the push-size prompt. `--yes` skips it. |
| `<id>: upload failed, nothing submitted: …` | Usually the VPN dropped; rerun `launch` (completed uploads are skipped). |
| `secret(s) not set in the environment: NAME` | Export the secret the job's `sky.yaml` requires. |
| `WARNING: secret NAME (sky.secrets) is not set in the environment; launching without it` | Export it if the job needs it, or drop it from `sky.secrets`. |
| `<id>: sky jobs launch failed (exit N)` | Read `sky`'s output above it (often policy or quota). The uploads stay in S3; rerunning `launch` reuses them. |

**Pull, logs, cancel**

| Message | Fix |
|---|---|
| `no S3 output for <id>` | The job never ran with `xr3-sky`, or never synced yet. |
| `<id>: in progress (no done marker in S3), skipping` | Wait until it finishes (`xr3-sky status`). |
| `WARNING: skipping unsafe S3 key '<key>'` | A key would land outside `output/`; it is skipped on purpose. |
| `no managed job named <user>-r3-<id8>` | Never launched under this user, or `sky jobs queue` no longer lists it. |

**In the pod** (`xr3-sky logs <id>`, lines start with `[xr3sky]`)

| Log line | Meaning / fix |
|---|---|
| `ERROR: job files incomplete in S3 (no …/files.complete)` | The push did not finish; rerun `launch`. The job is now marked `failed`, so use `launch -r`. |
| `ERROR: job <id> has no run.sh` | As above: the job files in S3 are incomplete. |
| `ERROR: commit <sha> not found in bundle for <repo>` | A bad bundle in S3: delete `git/<digest>/<sha>.bundle` and relaunch with `-r`. |
| `WARNING: r3 rebuild-index failed; continuing …` | Harmless: checkout does not need the index. |
| `job failed with exit N; writing output/done = failed` | `run.sh` failed: read the log above it, fix, commit a new version (or `launch -r` if it was transient). |
| `ERROR: run.sh exited 0 but output/done is '…', not 'completed'` | `run.sh` did not write `completed` (not the house `run.sh`, or its guard skipped the run). |
| `ERROR: final output sync failed; outputs may be missing from S3; output/done not uploaded` | S3 trouble at exit. The periodic syncs may have saved most of it; relaunch. |
| `bootstrap failed before output/ was restored from S3 (exit N); S3 left untouched` | Setup trouble (S3 credentials or endpoint in the pod); nothing in S3 changed. |
| `WARNING: output sync skipped some files (aws exit 2)` | Usually a dangling symlink in `output/`; those files are not in S3. |

## Known limitations

- **No multi-node jobs** (`num_nodes` must be 1).
- **GPU jobs pin one accelerator**, and so one cluster; they cannot float to whichever
  cluster has a free GPU.
- **The VPN is needed for push and pull** (`launch`, `launch --dry`, `pull`): the RGW is
  not reachable from the laptop through Teleport. Monitoring (`status`, `logs`, `cancel`)
  works without it.
- **An infrastructure failure marks the job `failed`.** A bootstrap or `run.sh` failure
  that is not the job's fault (an S3 hiccup, a failed `uv sync` download) looks the same
  as a real failure. Recover with `xr3-sky launch -r <id>`. Preemption and `cancel` do
  not mark `failed`.
- **The pod installs r3 from `github.com/mtangemann/r3` at your laptop's r3 commit.** If
  that commit is not on GitHub (a local or fork-only commit), `setup` fails.
- **No automatic pull.** Run `xr3-sky pull` yourself (a cron job calling it works).
- **Pointer jobs to Spiral depend on `asof` retention, which is unconfirmed.** See
  [`examples/pointer-job/`](../../examples/pointer-job/).
- **Foreman** integration (showing SkyPilot jobs next to SLURM ones) is separate work
  (design spec §5.7); until it lands, use `xr3-sky status`.
