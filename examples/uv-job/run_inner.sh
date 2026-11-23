#!/bin/bash
# Compute logic for a uv-lockfile environment: build the exact env from uv.lock, run main.py.
# Portable: the same script runs on a laptop, under SLURM, and in an xr3-sky pod.
set -euo pipefail
# --frozen: install exactly what uv.lock pins, never re-resolve or rewrite the lock
# (committed files are read-only, and a re-resolve would change the environment).
uv sync --frozen
uv run --frozen python main.py
