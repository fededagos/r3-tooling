---
name: xr3-sky
description: Use when running, launching, monitoring or pulling committed r3 jobs on SkyPilot / the lab's Kubernetes clusters (at, morpho, DO), when writing a job's sky.yaml, or on any `xr3-sky` command or error.
---

# xr3-sky — r3 jobs on the lab's SkyPilot clusters

`xr3-sky` launches a **committed** r3 job as a SkyPilot managed job. The r3 repo stays on
the laptop; Ceph S3 carries the job, its dependencies and its outputs both ways; the pod
runs the job's house `run.sh` unchanged. The pod is disposable: **S3 is the state.**

Full reference, in the r3-tooling repo (`~/r3-toolchain/r3-tooling` by default;
`realpath ~/.claude/skills/xr3-sky` finds it): `extensions/xr3-sky/README.md` (every flag,
the S3 layout, secrets, a troubleshooting table quoting every message) and
`extensions/CONTRACT.md` § "For `xr3-sky`" (what a job must satisfy). Read the README
before your first launch in a session, and whenever a message below is not enough.
Cluster questions (which GPU, VRAM per slot, priorities, what is free, `sky-up` itself)
belong to the `enigma-skypilot` skill.

## Preconditions

Check all three before `launch`; each failure has a one-line message:

- **Stanford VPN** up, for any S3 traffic (`launch`, `launch --dry`, `pull`).
  Message: `S3 unreachable — connect to the Stanford VPN`.
- **`sky-up`** run, for the SkyPilot API (`launch`, `logs`, `cancel`; `status` degrades
  without it and shows `?` for the managed state).
  Message: `SkyPilot API unreachable — run sky-up`.
- **`R3_REPOSITORY`** set. Also `sky.user` in `~/.config/xr3.yaml` or `$SKYPILOT_USER`.

The VPN and the Teleport tunnel coexist. Ask the human to connect the VPN; you cannot.

## The loop

```bash
xr3-sky launch --dry <id>      # upload plan + rendered task.yaml; writes nothing
xr3-sky launch <id>            # push + submit (detached): "submitted <name>; follow with: xr3-sky logs <id>"
xr3-sky status '<tag-glob>'    # done state (S3) + managed-job state
xr3-sky logs <id>              # stream; pod lines start with [xr3sky]
xr3-sky pull <id>              # after it finishes: S3 -> $R3_REPOSITORY/jobs/<id>/output
```

Select many jobs with `--tag`/`--query` on `launch` and `pull`, as with `xr3-slurm submit`.
`cancel <id>` stops the newest managed job for that id. A job is done when `status` shows
`completed` in the `done` column; `pull` then copies it. Always `--dry` first for a job
whose `sky.yaml` you just wrote.

The job must be launchable: committed, with the house `run.sh`, and an environment from
`pyproject.toml` + `uv.lock` (`run_inner.sh`: `uv sync --frozen` / `uv run --frozen`).
Copy `examples/uv-job/` to start one. `launch` refuses a job that depends on a container
or venv environment job.

## `sky.yaml`

A committed SkyPilot task fragment, merged over `sky.defaults`; the resources are versioned
with the job, so changing them means a new commit. Keys: `resources`, `config`, `envs`,
`secrets` (names only), `num_nodes` (1). `xr3-sky` refuses, before writing anything:

- a GPU job that does not pin **exactly one accelerator** (`accelerators: L40S:1`, not a
  list or `any_of`). The accelerator pins the cluster. CPU-only jobs may float, and
  `--infra k8s/<cluster>` narrows them.
- a HAMi-shared card (`RTXPRO6000`, `H100-80GB`, `L40S`) with no `nvidia.com/gpumem` cap
  under top-level `config.kubernetes.pod_config.spec.containers[0].resources.limits`.
  Request the slots the job needs. `--whole-card` books the whole card: use it only when
  the job really needs the whole card.
- `priorityClassName: high` without `--allow-high`. High preempts colleagues: ask the
  human, who asks the lab, before passing it.

## Done state

The S3 copy of `output/done` decides; the local copy counts only when S3 has none.

| State | `launch` | `-r` / `--restart-failed` | `--fresh` |
|---|---|---|---|
| none / `restart` | runs | runs | runs |
| `failed` | skips | rewrites to `restart`, runs | runs |
| `completed` | skips | skips | runs |

- A relaunch **keeps partial output**: the pod restores `output/` from S3 first, so a
  checkpointing job resumes.
- `--fresh` deletes the S3 output first. It destroys results: confirm with the human.
- The pod writes `failed` when `run.sh` exits non-zero, **including infrastructure
  failures** (an S3 hiccup, a failed `uv sync` download). Read `logs`; if the cause was
  transient, `launch -r`; if it was the job, fix it and commit a new version.
- Preemption and `cancel` never write `failed`; SkyPilot resumes a preempted job.
- A failure in SkyPilot's `setup:` phase (installing uv, r3 or the AWS CLI) never reaches
  the bootstrap, so it writes **no** `failed` marker; only `run:` does. The managed job
  shows `FAILED_SETUP` in `status` while the done state stays as it was; read
  `xr3-sky logs <id> --controller` (and `xr3-sky logs <id>`), fix, and relaunch.

## Outputs

`pull` leaves files over `--max-size` (default 100MB) in S3 and lists them in
`output/REMOTE.yaml` (path, size, etag, `s3://` uri). `pull --all` fetches everything. A
downstream `xr3-sky` job gets the full output from S3 anyway; a downstream job run
elsewhere needs `pull --all` first.

## Pointer jobs

For data that stays outside r3 (a Spiral table, a W&B artifact, an S3 object), commit a
**pointer job**: no `run.sh`, a `README.md`, and `output/pointer.yaml` with a `kind` and a
pinned version. Downstream jobs fetch the data themselves; `launch` pushes the tiny manifest
like any dependency. Spiral `asof` retention is unconfirmed: point at a frozen copy for
anything long-lived. Template: `examples/pointer-job/`.

## Common errors

| Message (abridged) | Fix |
|---|---|
| `<id>: skipping (completed)` | Intended. `--fresh` only if a rerun is really wanted. |
| `<id>: skipping (failed)` | Read `xr3-sky logs <id>`, then `launch -r` or fix and recommit. |
| `… needs a nvidia.com/gpumem cap: one slot is S MiB, whole card is C MiB …` | Add the cap to `sky.yaml` (a new commit), or `--whole-card`. |
| `GPU jobs must pin exactly one accelerator` | One `accelerators: NAME:count`. |
| `depends on a container/venv environment job <dep>` | Move the job to a uv lockfile (`examples/uv-job/`). |
| `dependency <dep> has no output locally or in S3` | Run or `pull` the dependency first. |
| `dependency <dep> is unfinished (no output/done); …` | Finish it; `--allow-unfinished-deps` only on purpose. |
| `already queued/running as managed job N (…)` | Wait or `cancel`; `--force-duplicate` only on purpose. |
| `secret(s) not set in the environment: NAME` | Export the secret the job's `sky.yaml` lists. |
| `<id>: in progress (no done marker in S3), skipping` | Not finished: check `status`, pull later. |
| `No run script in <dir>.` | Pointer/raw-data jobs are not launchable; others need the house `run.sh`. |

Everything else: the troubleshooting table in `extensions/xr3-sky/README.md`.
