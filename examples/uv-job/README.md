# uv-job — a compute job whose environment is a uv lockfile

A **compute** job that carries its own Python environment: `pyproject.toml` lists the
dependencies, `uv.lock` pins them exactly, and `run_inner.sh` builds that environment with
`uv sync --frozen` before running `main.py`. uv runs everywhere, so the same committed job
runs unchanged on a laptop, under SLURM, and in a SkyPilot pod through
[`xr3-sky`](../../extensions/xr3-sky/README.md). This is the environment shape `xr3-sky`
requires; the container and venv environment jobs ([`../environment/`](../environment/))
cannot run in a Linux pod.

## Files

- `run.sh` — the house portable runner, verbatim from [`../compute-job/`](../compute-job/).
- `run_inner.sh` — `uv sync --frozen` then `uv run --frozen python main.py`. `--frozen`
  installs exactly what `uv.lock` pins and never re-resolves.
- `main.py` — your computation. The stub writes `output/result.txt`.
- `pyproject.toml` + `uv.lock` — the environment. No `[build-system]`, so uv installs the
  dependencies only.
- `sky.yaml` — resources for `xr3-sky launch` (here CPU-only: 1 CPU, 2 GB).
- `r3.yaml` — the job's dependencies (none; commented examples show a dataset and a git
  dependency).
- `metadata.yaml` — editable labels; `tags[0]` is the primary version tag.

## Use

1. Replace the `PROJECT` / `DATE_SLUG` / `USER` / `CLUSTER` placeholders.
2. Set your dependencies in `pyproject.toml`, then run **`uv lock`** and commit the lock
   with the job. Changing a dependency means a new lock and a new job version.
3. Try it: `bash run_inner.sh` (remove the `.venv/` and `output/` it creates before committing).
4. `xr3 check . && xr3 commit .`, then run it anywhere:
   - laptop: `run_job_locally <committed-dir>`;
   - SLURM: `xr3-slurm submit <id>`;
   - SkyPilot: `xr3-sky launch <id>`, then `xr3-sky pull <id>`.

For a GPU job, edit `sky.yaml` (one accelerator, plus a `nvidia.com/gpumem` cap on shared
cards); the rules are in the [xr3-sky README](../../extensions/xr3-sky/README.md#skyyaml--resources-versioned-with-the-job).

## Notes

- The environment is rebuilt per run (about a minute in a fresh pod). uv's cache makes
  repeat runs on the same machine fast.
