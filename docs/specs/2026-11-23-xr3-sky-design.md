# xr3-sky + foreman SkyPilot backend — design spec

- **Date:** 2026-11-23
- **Status:** approved design; ready for implementation plan
- **Repos:** `r3-tooling` (public fork `fededagos/r3-tooling`, branch `xr3-sky`) and
  `foreman` (private fork `fededagos/foreman`, branch on top of `feature/slurm-queue`)
- **Related:** `extensions/xr3-slurm/` (the sibling this mirrors), `extensions/CONTRACT.md`,
  `RESEARCH_WORKFLOW.md` §4 (run.sh), foreman `foreman/slurm.py`, the `enigma-skypilot` skill

## 1. Intent

Run committed r3 jobs on the lab's SkyPilot Kubernetes clusters (`at`, `morpho`, `DO`)
with the same convenience `xr3-slurm` gives on MLCloud, and see them in foreman.
The r3 repository stays on the laptop. Job outputs must outlive the pods, which are
disposable, and come back to the laptop: either as files or, for large or external
things, as versionable pointers.

Many inputs are not files at all: Spiral tables, W&B runs, checkpoints in S3. These should
still appear in the r3 provenance graph as dependencies.

## 2. Goals, non-goals, anti-goals

**Goals**

1. `xr3-sky launch` submits committed r3 jobs as SkyPilot managed jobs, on any of the
   three clusters, running the job's `run.sh` unchanged.
2. Outputs are synced to S3 during the run and at the end (including on failure).
   `xr3-sky pull` brings them into `~/r3_repo/jobs/<id>/output`.
3. Resource requests (GPU, VRAM cap, priority, cluster) are versioned with the job.
4. The environment is defined by a lockfile in the job (`uv sync --frozen` in the pod).
5. Foreman shows SkyPilot jobs (live state, history, queue page) the way it shows Slurm jobs.
6. A documented **pointer-job** convention for external artifacts.

**Non-goals (v1)**

- Automatic pulling. A cron job calling `xr3-sky pull` is the escape hatch.
- Multi-node jobs.
- Dev-box orchestration beyond what the uv lockfile already gives.
- A per-cluster `~/persist` cache for large dependency outputs (possible later).
- A `pointer` helper command. v1 ships the convention plus an example only.
- Replacing or changing `xr3-slurm`, or upstreaming anything.

**Anti-goals (failure even if it "works")**

- Losing outputs: a job that fails or is preempted must not take its partial output with it.
- An uncapped GPU job on a HAMi cluster (morpho, DO, at `tm-gpu0`). Uncapped jobs book the
  whole card but take one slot, which misleads colleagues.
- Secrets (AWS, W&B, Spiral) ending up in job files, S3 objects, the r3 repo or logs.
- Needing the VPN just to *watch* jobs. Monitoring goes through Teleport only.

## 3. Constraints (verified 2026-11-19)

- **S3:** Ceph RGW cold, `https://rgw-cold.enigmatic.stanford.edu`, path-style addressing,
  region `stanford`. Bucket `federico-r3` (owned by the user, versioning **off**).
  - Credentials: the personal portal key, available locally as AWS profile `enigma`.
  - The laptop can reach it **only on the Stanford VPN**. A Teleport route has been asked
    for, but there is no answer yet.
  - Pods on `at` and `DO` both reach the public endpoint. `at` also resolves
    `rgw-cold.rook-ceph.svc`. `DO` (Paperspace NY) has no in-cluster endpoint.
- **SkyPilot 0.13.0**, API server reached via Teleport (`sky-up`, `localhost:46581`).
  - **DO accepts managed jobs only:** policy rejects `sky launch`.
  - DO has no persistent volume.
  - Cluster and job names must contain the user's name.
  - `sky jobs queue -o json [-u] [--skip-finished]` exists.
- **HAMi `gpumem` caps** (MiB): morpho `9788`/`97887`, DO `8155`/`81559`, at L40S
  `4606`/`46068` (one slot / whole card).
  - `config:` is top-level in the task YAML.
  - Priority classes are `low`/`medium`/`high`. `high` needs asking people first.
- **Ephemeral disk** is limited to 32 GiB by default (DO: 100 GiB hard cap).
  `ephemeral_storage:` raises it.
- **r3 checkout semantics** (`r3/storage.py:218-310`):
  - It copies the job's files.
  - It symlinks `output/` and each dependency's source into `<repo>/jobs/<dep>/…`.
  - It fetches git dependencies with `git fetch --depth=1 <repo>/git/<path> <commit>`.
  - Committed files are read-only.
  - `r3 rebuild-index` exists.
- The **house `run.sh`** (`examples/compute-job/run.sh`):
  - Takes `R3_JOB_ID` from the directory name and checks out into `${SCRATCH:-$(mktemp -d)}/job`.
  - Runs plain `bash run_inner.sh` when `SLURM_JOB_ID` is unset.
  - Writes `output/done` at the end.
- r3 needs Python `<3.13`. The laptop `sky` CLI is a separate uv tool (Python 3.9).
- **The Stanford VPN and the Teleport tunnel can be up at the same time.** The
  2026-11-19 round-trip test pushed to S3 and launched through `sky` in one session, so
  `launch` can be a single command.
- **There is exactly one r3 repository:** `~/r3_repo` on the laptop. This install is
  `--no-slurm`, and there is no MLCloud repository in play. A dependency whose output is
  missing locally is an error (§5.4), not something to fetch from elsewhere.

## 4. Approaches considered

**Where the repo lives and how bytes move**

| | Approach | Trade-off |
|---|---|---|
| **✔ 1** | **Laptop keeps the repo; S3 carries files both ways** | Works on all three clusters and lets jobs float between them. Large dependency outputs go through S3, once each. Needs the VPN to push and pull. |
| 2 | Mirror of the repo on `~/persist`, pulled back through an always-on sync pod | No S3, and large data stays put. But it is pinned to at *or* morpho, never DO, and leaves two repos to keep consistent. |
| 3 | Repo on a cluster dev box, laptop holds pointers only | Simplest, but outputs never reach the laptop, and it only works on at. |

**Picked: 1.** Most data lives in Spiral and other external stores anyway, so the
"large deps through S3" cost is small once pointer jobs exist.

**Environment in the pod**

| | Approach | Trade-off |
|---|---|---|
| **✔ 1** | **`pyproject.toml` + `uv.lock` in the job; `uv sync --frozen` at pod start** | The environment is versioned exactly with the job and works for dev boxes too. Costs about a minute of install per run. |
| 2 | Docker image per project, pinned by digest | Fastest start, but needs a registry and the environment is defined outside r3. |
| 3 | Environment as its own r3 job (house venv/container pattern) built on the cluster | Most r3-pure, but large environments go through S3 and need a separate build job first. |

**Picked: 1** ("also if I want to do some dev work using r3").

**Where resource requests live:** in the job (picked) or as launch flags like
`xr3-slurm submit --partition`. Picked: in the job, with defaults in `~/.config/xr3.yaml`.

**Pulling outputs:** a manual `xr3-sky pull` (picked). Automatic pulling is deferred to an
optional cron job.

## 5. Design

### 5.1 S3 layout

```
s3://<bucket>/<prefix>/                      # default bucket federico-r3, prefix r3
  jobs/<job-id>/files/…                      # committed job files (no output/), uploaded once
  jobs/<job-id>/output/…                     # job output: synced from the pod, or pushed from the laptop for deps
  jobs/<job-id>/output.complete              # marker: output/ upload is complete and final
  git/<sha256(repository_path)>/<commit>.bundle   # git dependency at one commit
  runs/<job-id>/<launch-ts>/task.yaml        # the exact rendered SkyPilot task, for debugging
```

Committed jobs are immutable, so an object that already exists is never re-uploaded
(check `output.complete` or the `files/` prefix before uploading).

### 5.2 Job-side contract: `sky.yaml`

A committed job may contain `sky.yaml`: a **fragment of a SkyPilot task YAML** that
`xr3-sky` merges over the defaults from `~/.config/xr3.yaml` (`sky:` section).
Allowed keys:

- `resources`
- `config` (e.g. `gpumem`, priority, `provision_timeout`)
- `envs`
- `secrets` (names only, values never)
- `num_nodes`: must be 1 in v1

`xr3-sky` owns `name`, `setup`, `run` and `file_mounts`. A job without `sky.yaml` uses
the defaults alone.

**Placement rule (call):**

- A **GPU job must pin exactly one accelerator** in `sky.yaml`. Every accelerator exists
  on only one cluster, so this also pins the cluster.
  - `xr3-sky` looks the accelerator up in a table that records whether it is HAMi-shared
    and what its one-slot and whole-card `gpumem` values are.
  - It validates or fills in the cap from that table.
  - A job listing several accelerators to choose from (`any_of` / `ordered`) is refused
    in v1.
- **CPU-only jobs** with no `volumes:` may float between clusters (`any_of` allowed).
- `--infra` on `launch` may only **narrow** what `sky.yaml` allows. For example, it can
  pick one cluster for a floating CPU job. It may never contradict `sky.yaml`.
- Where a job actually ran is recorded in `runs/<id>/<ts>/task.yaml` and in the SkyPilot
  job record. It does not go in the committed job, which stays immutable.

**Environment rule (call):**

- `xr3-sky` is for jobs whose environment comes from a uv lockfile: `pyproject.toml` +
  `uv.lock` in the job or a git dependency, with `run_inner.sh` using `uv sync --frozen`
  / `uv run`.
- That `run_inner.sh` stays portable to the laptop and Slurm, because uv runs everywhere.
- `launch` **refuses** a job whose transitive dependencies include a house environment
  job. These are detected by a dependency output containing `container.sif` or
  `env/interpreter_path.txt`. Such environments can't run in a Linux pod (macOS venv, no
  Singularity), and uploading them would waste gigabytes.
- v1 ships a `examples/uv-job/` skeleton and a short "uv-lockfile environment" entry in
  `RESEARCH_WORKFLOW.md` §5. It sits *alongside* the container/venv archetypes and does
  not replace them.

**Validation before launch (refuse with a clear message):**

- A GPU request on a HAMi cluster without a `nvidia.com/gpumem` limit. `--whole-card`
  fills in the whole-card value from a table keyed by accelerator.
- `config:` nested inside `resources:`.
- `priorityClassName: high` without `--allow-high`.
- Any `volumes:` entry naming `do-*`.

### 5.3 `xr3-sky` CLI

`extensions/xr3-sky/xr3-sky` plus a pure, unit-tested `xr3skylib.py`, mirroring
`xr3-slurm`.

| Command | Behaviour |
|---|---|
| `launch JOB_IDS… [--tag/--query] [--dry] [--infra …] [--whole-card] [--allow-high]` | Same selection, de-duplication and `output/done` idempotency as `xr3-slurm submit`, including `--restart-failed`. Steps: preflight (§5.6), push (§5.4), render the task, `sky jobs launch -y -n <name> <task>`. `--dry` prints the rendered YAML and the upload plan. |
| `pull JOB_IDS… [--tag] [--max-size 100MB] [--all]` | `aws s3 sync` of `jobs/<id>/output/` into `~/r3_repo/jobs/<id>/output/`. Files over `--max-size` are not downloaded; instead they are listed in `output/REMOTE.yaml` (key, size, etag, `s3://` URI). `--all` ignores the limit. |
| `status [GLOB]` | Like `xr3-slurm status`: the `output/done` state (local, or from S3 when not pulled yet) next to the managed-job state from `sky jobs queue -o json`. |
| `logs JOB_ID` / `cancel JOB_ID` | Thin wrappers around `sky jobs logs` / `sky jobs cancel`, resolving the r3 id to the newest managed job of that name. |

**Managed-job name:** `<user>-r3-<first 8 of id>`, because names must contain the user.
The full id goes in the env var `R3_JOB_ID` and in the job name's index in foreman (§5.7).
Builder: if SkyPilot allows names long enough and the policy accepts them, prefer
`<user>-r3-<full id>`.

**Done state and restarts (call):**

- **S3 is authoritative** for a job that has ever been launched with `xr3-sky`, meaning
  `jobs/<id>/output/` exists in S3. The local `output/` is a copy made by `pull`.
- `launch` reads `jobs/<id>/output/done` from S3 when it exists, and otherwise the local
  marker. Otherwise it follows `xr3-slurm submit` exactly:
  - skip `completed`;
  - re-run `restart`;
  - skip `failed` unless `--restart-failed` is given.
- For `--restart-failed`, `launch` **writes `restart` into the S3 marker before
  submitting**, and into the local copy if one exists. This way the house `run.sh` guard
  lets the job through once the pod has pre-populated `output/` from S3.
- **Partial output is kept on restart.** Resuming from checkpoints is the job's business
  (RESEARCH_WORKFLOW §10). A job that wants a clean slate clears its own output.
  `--fresh` on `launch` deletes the S3 `output/` first, which needs the bucket's delete
  permission (the user owns it).

### 5.4 Push (laptop → S3), done by `launch`

For the job and the **transitive closure** of its dependencies:

1. **Job files:** upload `jobs/<id>/` except `output/` to `files/`, if not present.
2. **Job dependencies:** upload `output/` (or just the referenced `source` subpaths) if
   `output.complete` is missing.
   - A dependency whose local `output/` contains `REMOTE.yaml` was produced on SkyPilot,
     so its full output is already in S3 and nothing needs uploading.
   - A dependency with no `output/done` triggers a warning (`--allow-unfinished-deps`
     proceeds).
   - A dependency whose output is neither local nor complete in S3 is an error that names
     the dependency.
3. **Git dependencies:** `git bundle create` with exactly the pinned commit from
   `<repo>/git/<path>`, uploaded once per commit.
4. **Size guard:** print a total; above a threshold (config, default 5 GB), require `--yes`.

### 5.5 In the pod

The rendered task:

- **`setup`:**
  - Install `uv` if missing.
  - Install `r3` with uv, pinned to the laptop's r3 commit (recorded at launch), plus `awscli`.
  - Set `UV_CACHE_DIR` on local disk.
- **`run`:** a bootstrap script shipped in `xr3-sky` and inlined into the task.
  1. Build a **sparse r3 repo** at `$HOME/r3_repo` (local disk):
     - `r3.yaml`, plus `jobs/<id>/` for the job and every job dependency, downloaded from S3.
     - `git/<path>` populated by cloning the bundles.
     - Then `r3 rebuild-index`.
  2. Make `jobs/<id>/output/` a writable local directory, pre-populated from S3 if the job
     has earlier partial output (resume after preemption or restart).
  3. Start a background loop: `aws s3 sync output/ …/output/` every `sync_interval`
     (default 5 min).
  4. `cd jobs/<id> && R3_REPOSITORY=$HOME/r3_repo bash run.sh`. This is the house `run.sh`,
     unchanged, and its `run_inner.sh` does `uv sync --frozen` / `uv run`.
  5. On **any exit** (a `trap`), do a final sync.
     - If `run.sh` failed and no `output/done` exists, write `failed` to it before syncing,
       so `--restart-failed` works.
     - Then write `output.complete`, but only when `output/done` is `completed`.
     - Exit with `run.sh`'s status.
- **Endpoint:** use `rgw-cold.rook-ceph.svc` when it resolves (at), otherwise the public
  endpoint.
- **Secrets:** `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` passed with `--secret` from the
  `enigma` profile. Extra secret names (`WANDB_API_KEY`, `SPIRAL_*`) come from config or
  `sky.yaml`, with values read from the laptop environment.
- **Managed-job recovery:** SkyPilot restarts the task on preemption, and step 2 resumes
  from the last sync.

### 5.6 Preflight

`launch`, `pull` and `status` check, with a one-line fix for each failure:

- S3 is reachable: on failure, say "connect to the Stanford VPN".
- The `sky` API is healthy: on failure, say "run `sky-up`".
- `R3_REPOSITORY` is set.
- The profile has credentials.

`status` degrades gracefully: without the VPN it shows the SkyPilot state only.

### 5.7 Foreman SkyPilot backend

On a branch of `fededagos/foreman` on top of `feature/slurm-queue`.

- Extract a small **scheduler interface** from `Slurm`: `get_jobs()` (live state by key),
  `get_history()` / `get_records()`, `get_queue()`, `check_host()`.
  - `Slurm` becomes one implementation.
  - `SkyPilot` is the other, shelling out to `sky jobs queue -o json` (all jobs) and
    `--skip-finished`, with the same `diskcache` memoization.
  - A composite combines the configured backends, so `r3_extension.status` and the queue
    page see both.
- **Matching:** a managed job matches an r3 job by its name suffix or by `R3_JOB_ID` in its
  envs (whichever `sky jobs queue -o json` exposes; the builder checks).
- **State mapping:**

  | SkyPilot state | Foreman status |
  |---|---|
  | `PENDING`, `STARTING` | queued |
  | `RUNNING`, `RECOVERING` | running |
  | `SUCCEEDED` | completed |
  | `FAILED*` | failed |
  | `CANCELLING`, `CANCELLED` | cancelled |
- **Queue page:** SkyPilot jobs appear with columns for infra/cluster, resources (the
  accelerator), state, submitted, start and elapsed, plus a link to the SkyPilot dashboard
  job page (`http://localhost:46581/dashboard/jobs/<id>`).
- **Settings page:** an enable switch, the cache expiry, and the `sky` binary path
  (default: `which sky`).
- Foreman runs on the laptop and needs the Teleport tunnel (`sky-up`), not the VPN.
- **Out of scope for foreman v1:** launching, pulling, or showing S3-only outputs.

### 5.8 Pointer jobs

A convention, documented in `RESEARCH_WORKFLOW.md` (archetypes) and
`examples/pointer-job/`:

- A committed job with no `run.sh`, a `README.md`, and `output/pointer.yaml`.
- `pointer.yaml` has a `kind` field plus the kind's fields:
  - **`spiral`:** `project`, `dataset`, `table`, `asof_us`, and a `content_sha256` of the
    scanned data, if feasible.
  - **`wandb`:** `entity/project/artifact:version` or a run path.
  - **`s3`:** a URI plus a version id (or etag) when the bucket is versioned.
- Downstream code reads the manifest and fetches the data itself.
- Pointer outputs are tiny, so they are pushed through S3 like any other dependency.
- **Caveat** (documented): Spiral tables are mutable and `asof` reads depend on retention
  of old versions, which is unconfirmed. For long-lived reproducibility, freeze a copy (the
  spine `*_initiative1` pattern) and point at that instead.

### 5.9 Config (`~/.config/xr3.yaml`, new `sky:` section)

```yaml
sky:
  bucket: federico-r3
  prefix: r3
  endpoint: https://rgw-cold.enigmatic.stanford.edu
  aws_profile: enigma
  sync_interval: 300
  pull_max_size: 100MB
  push_confirm_above: 5GB
  secrets: [WANDB_API_KEY]          # names; values from the laptop env
  defaults:                          # SkyPilot task fragment, merged under each job's sky.yaml
    resources: {cpus: 4, memory: 16}
```

### 5.10 Docs & install

- `extensions/xr3-sky/README.md`.
- A new **"For `xr3-sky`"** section in `CONTRACT.md`: `sky.yaml`, the `run.sh` and
  `R3_REPOSITORY` assumptions, the uv lockfile, the VPN for push and pull, and the
  `output/done` + `failed` rule.
- `install.sh`: write an `xr3-sky` wrapper. Without that change the wrapper is written by
  hand on this machine.
- Point the canonical `r3-tooling` clone at the fork (`origin` = fork, `upstream` =
  `matthias-k`), so `update.sh` keeps working.

## 6. Left to the builder

- Exact name length or characters SkyPilot accepts for managed jobs; how to read envs back
  from `sky jobs queue -o json`.
- Whether `aws s3 sync` or boto3 is used in the laptop CLI. Prefer boto3 with one pooled
  client (the wiki's throughput advice), and the `aws` CLI inside the pod.
- Whether `r3 rebuild-index` works on a sparse repo, or the index must be shipped too.
- The test strategy: unit tests for `xr3skylib.py` (rendering, validation, merge, S3 key
  layout, `REMOTE.yaml`); one end-to-end smoke test on `at` with a CPU-only job, using a
  tiny job that depends on one job and one git dependency.
