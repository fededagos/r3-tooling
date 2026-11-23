# CONTRACT — what the extensions assume about your r3 jobs

**Read this before using `xr3` / `xr3-slurm` / `xr3-sky` on your jobs.** Bare [r3](https://github.com/mtangemann/r3)
makes almost no assumptions about a job's contents. The house tools in `extensions/` add a few — they buy
you real convenience (see each tool's README for the pitch), but a job that violates an assumption will
make the relevant command fail or misbehave. This file is the **single source of truth** for those
assumptions; the tools link here from their gating errors and `--help`.

Each item says **what it is** and **what breaks if you don't follow it**. Nothing here is required by r3
itself — only by these tools.

---

## Always required (nearly every command)

- **`R3_REPOSITORY` points at your r3 repo.** Set the env var (some commands also accept `--repository` or
  a `REPOSITORY_PATH` argument). Nearly everything needs it: all of `xr3` except `files`, and `xr3-slurm`
  `submit`/`status`/`watch --tag`, and `xr3-sky` `launch`/`pull`/`status` — the exceptions are plain
  `xr3-slurm watch <name/id>`, which talks only to SLURM, and `xr3-sky logs`/`cancel`, which talk only to
  SkyPilot. *Breaks:* the command errors without it — `dev-checkout`/`dev-cleanup`/`git-check` with a clean
  "Missing argument 'REPOSITORY_PATH'", the rest (including `diff`, `find`, `check`) with a raw traceback.
- **Python 3 + runtime deps on `PYTHONPATH`.** `xr3` needs `click`, `pyyaml`, `r3`, `executor`; `xr3-slurm`
  additionally needs `tqdm`; `xr3-sky` needs `boto3` (and the `sky` CLI on `PATH`). *Breaks:* `ImportError` at startup. (Both tools are single-file scripts run
  through your r3 Python environment.)

## For the pathmap-dependent workflow commands (`history`, `diff`, `check`, `commit`)

- **Your working directory maps to an r3 logical path.** These commands derive the job's r3 path from
  *where the working directory lives on disk*, via **pathmap roots** you configure (see
  [xr3 config](xr3/README.md)). Put your project roots in `~/.config/xr3.yaml` (or `$XR3_CONFIG`).
  *Breaks:* the command exits with a `No pathmap root matches <path> … (see CONTRACT.md)` error. (`find` and
  `files` do **not** need pathmap. Also skip it: `diff --compare-to <id>` compares two committed jobs
  directly, and `commit --no-check` skips the check that resolves the path.)

  A `pathmap.roots` entry with no `prefix` is a **base root**: subpaths map directly
  (the first sub-directory becomes the first r3-path segment), so a single base root over
  your projects directory covers every project beneath it, and multiple base roots share
  one path space. `~` and `$VARS` in `path` are expanded. Running xr3 in a directory that
  *is* a root (no sub-path) is an error.
- **`metadata.path` matches the job's logical location**, and **`tags[0]` = `<path>/vX.Y.Z`** (the primary
  version tag is the path plus a semver). *Breaks:* `xr3 check` fails its path- and tag-consistency asserts
  (and `commit` runs `check` by default).
- **No `bug/…`-tagged dependencies** (configurable via `blockers.tags`). *Breaks:* `check` and
  `dev-checkout` refuse the job (`dev-checkout --allow-bugs` overrides).
- **No non-empty `metadata.WIP`** (toggle with `blockers.block_on_wip`). *Breaks:* `check` fails — and
  `commit`, which runs `check` by default. (`dev-checkout` does *not* check WIP.)

> Roadmap: `history`/`diff` could instead read `metadata.path` straight from the working dir and skip
> pathmap entirely — see the design spec §10. Until then, they need a pathmap root.

## For `xr3-slurm` (SLURM submission / observation — MLCloud/galvani)

- **A `slurm:` config section.** headnodes, `submit_host` (the sbatch SSH target), node excludes, and
  partition/mem defaults live in `~/.config/xr3.yaml` (see [xr3-slurm](xr3-slurm/README.md)). *Breaks:*
  wrong cluster / no submission host. This is **MLCloud-specific** (galvani today; ferranti and other
  SLURM clusters via config) — not cluster-agnostic like `xr3`.
- **SSH access to the `submit_host`.** `sbatch` (and, by default, `squeue`/`sacct` polling) runs over SSH
  to the headnode. *Breaks:* submission/observation fail if you can't reach the headnode.
- **Committed jobs (dir named by UUID) with a `run.sh`.** `submit` operates on committed r3 job
  directories (whose name is the job's UUID) and submits their `run.sh`. *Breaks:* "not a valid UUID" /
  "No run script".
- **The `output/done` marker convention.** A job's `run.sh` writes `output/done` on completion. The body is
  normally a done sentinel; the special bodies **`restart`** and **`failed`** drive control flow:
  - `submit` **skips** a job whose `output/done` exists (idempotent resubmit), **except** body `restart`
    (always resubmits) or body `failed` with `--restart-failed` (rewrites to `restart`, resubmits).
  - `status` reports `done` / `restart` / `failed` from this file.

  *Breaks:* without the marker, `submit` re-runs finished jobs and `status` can't show completion; the same
  marker is what `xr3 commit --copy-previous-output` / `--exclude-done` rely on.
- **Jobs `xr3-slurm` submits are findable by their r3 id.** `submit` sets `--job-name=<r3 job id>`; `status`
  and `watch` correlate SLURM jobs back to r3 jobs by that name. *Breaks:* if you submit some other way
  without `--job-name=<r3 id>`, `status`/`watch` won't match those jobs.

## For `xr3-sky` (SkyPilot managed jobs — lab Kubernetes)

`xr3-sky` runs a committed job in a disposable pod, with Ceph S3 as the only state that
outlives it (see [xr3-sky](xr3-sky/README.md)). What that demands of a job and a laptop:

- **Committed jobs (dir named by UUID) with the house `run.sh`.** `launch` operates on committed job
  directories and the pod runs `R3_REPOSITORY=<pod repo> bash run.sh` in the job dir, unchanged. The
  house `run.sh` (`examples/compute-job/run.sh`) checks the job out via `r3 checkout` and runs
  `run_inner.sh` with plain `bash` (no `SLURM_JOB_ID` in a pod). *Breaks:* "not a valid UUID" / "No run
  script"; a `run.sh` that assumes SLURM, Singularity or a host path fails in the pod.
- **`R3_REPOSITORY` is honoured, not hard-coded.** The pod builds a sparse repo at `$HOME/r3_repo`
  holding only the job, its dependencies and its git dependencies, and sets `R3_REPOSITORY` to it.
  *Breaks:* a job that reads other jobs by absolute path, or outside its declared dependencies, finds
  nothing there.
- **The environment comes from a uv lockfile.** `pyproject.toml` + `uv.lock` in the job (or a git
  dependency), and `run_inner.sh` uses `uv sync --frozen` / `uv run`; see `examples/uv-job/`. *Breaks:*
  `launch` refuses a job whose transitive dependencies include a container or venv environment job
  (an output holding `container.sif` or `env/interpreter_path.txt`): those cannot run in a Linux pod.
- **`sky.yaml` (optional) is a SkyPilot task fragment with only `resources`, `config`, `envs`,
  `secrets`, `num_nodes`.** It is merged over `sky.defaults` and validated: a GPU job pins exactly one
  accelerator; a HAMi-shared card (RTXPRO6000, H100-80GB, L40S) has a `nvidia.com/gpumem` cap unless
  `--whole-card`; `config:` is top-level; `num_nodes` is 1; priority `high` needs `--allow-high`.
  `secrets` holds names only (a value is refused: the file is pushed to S3). *Breaks:* `launch`
  refuses the job before writing anything.
- **The `output/done` marker, with `failed`.** As for `xr3-slurm`: `run.sh` writes `output/done`
  (`completed`) on success, and `launch` skips finished jobs, reruns `restart`, and reruns `failed` only
  with `--restart-failed` (rewriting the marker to `restart`). In addition the pod **writes `failed`**
  itself when `run.sh` exits non-zero with no marker (or `restart`), and the S3 copy of the marker wins
  over the local one. The laptop, the pod, `xr3-slurm` and foreman classify a body alike: `restart`,
  `failed`, anything else (empty included) is completed. `pull` copies a completed job only once the pod
  has written `output.complete`, skips one being rerun (`restart`), and copies a `failed` one as it is.
  *Breaks:* without the marker every `launch` reruns the job, `pull` treats it as in progress and
  downloads nothing, and the pod fails a run that exits 0 without a completed marker.
- **Partial output survives a relaunch.** The pod restores `output/` from S3 before `run.sh` starts, so
  resuming from checkpoints is the job's business (and a job that wants a clean slate clears its own
  output, or you pass `--fresh`). *Breaks:* a job that assumes an empty `output/` may mix runs.
- **S3 access from the laptop: the Stanford VPN plus the `sky.aws_profile` credentials.** `launch`
  pushes and `pull` downloads through the Ceph RGW, which the laptop reaches only on the VPN.
  *Breaks:* "S3 unreachable — connect to the Stanford VPN". Monitoring (`status`, `logs`, `cancel`)
  needs only the Teleport tunnel (`sky-up`).
- **Dependencies are finished and present.** Every job dependency has output locally (with
  `output/done`) or complete in S3; git dependencies are pinned to a commit present in the local
  mirror. A dependency with a local `REMOTE.yaml` must be complete in S3. *Breaks:* `launch` names the
  dependency and refuses (`--allow-unfinished-deps` pushes an unfinished one on purpose, with no
  `output.complete`, so it is re-uploaded on every launch; `--repush-deps` re-uploads finished ones).
- **Secrets travel as names.** Values come from the laptop environment (AWS from the profile) and are
  passed with `sky jobs launch --secret`. *Breaks:* a secret placed in `envs:` or a job file is written
  to S3 in clear text.
- **Managed jobs are named `<user>-r3-<first 8 chars of the r3 id>`**, with the user from `sky.user`,
  else the local part of `$SKYPILOT_USER` (foreman uses the same order after its own setting). `status`, `logs` and `cancel` find jobs by that name. *Breaks:* a job launched
  another way is invisible to them.

## For `xr3 diff`

- **GNU `diff`, a pager (`less`), and `PAGER`.** `diff` shells out to the system `diff` and pipes through a
  pager. This is fine on the Linux HPC; **not** portable to macOS/BSD `diff`. *Breaks:* diff rendering off
  the HPC. (In this sense `diff` is "portable" only within the HPC.)

## For `scripts/run_job_locally`

*(What the helper does is in [extensions/README.md](README.md); this is only what a job/environment must
provide for it to work.)*

- **The job's `run.sh` checks out into `${SCRATCH:-…}/job`.** The helper sets and **exports** `$SCRATCH`
  (the node-local SLURM scratch `/scratch_local/<user>-$SLURM_JOB_ID/` when you're in an allocation, else a
  fresh `mktemp -d`), then runs `run.sh`. The two agree only if `run.sh` reads `$SCRATCH` the same way — the
  house `run.sh` template does (`${SCRATCH:-$(mktemp -d)}/job`). *Breaks:* a `run.sh` that checks out
  elsewhere still runs, but the leftover-detection and scratch reuse silently don't apply.

  No SLURM allocation is required — outside SLURM the helper just makes a temp dir. It runs `run.sh` and
  tees stdout+stderr to `output/run_manual_<ts>.log`, so a manual run is logged like a submitted one.

---

*If a tool ever demands something not listed here, that's a doc bug — please add it.*
