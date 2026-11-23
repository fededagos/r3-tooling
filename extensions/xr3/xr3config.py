"""Configuration loading for the xr3 tool suite (xr3 + xr3-slurm).

Pure logic (stdlib + pyyaml only) so it is unit-testable without r3/executor.
Each tool reads only its own section(s). See xr3pathmap for the
working-dir->r3-path mapping.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "xr3.yaml"

DEFAULTS = {
    "pathmap": {"roots": []},
    "blockers": {"tags": ["bug/"], "block_on_wip": True},
    "dev_checkout": {"ignored_destinations": [], "ignored_repositories": []},
    "slurm": {
        "headnodes": ["galvani"],   # squeue/sacct polling targets (status, watch --tag)
        "submit_host": "galvani",   # sbatch SSH target; null/"" => submit locally
        "exclude_nodes": [],        # sbatch --exclude nodes (empty => no --exclude)
        "partition": None,          # default sbatch --partition (null => omit)
        "mem": None,                # default sbatch --mem (null => omit)
    },
    "sky": {
        "bucket": "federico-r3",
        "prefix": "r3",
        "endpoint": "https://rgw-cold.enigmatic.stanford.edu",
        # Opt-in in-cluster endpoint for pods; null => public `endpoint` only.
        # (rgw-cold.rook-ceph.svc resolves on at but does not serve the bucket.)
        "endpoint_internal": None,
        "aws_profile": "enigma",
        "user": None,               # null => local part of $SKYPILOT_USER
        "sync_interval": 300,
        "pull_max_size": "100MB",
        "push_confirm_above": "5GB",
        "secrets": ["WANDB_API_KEY"],   # names; values from the laptop env
        "allow_float_gpu": False,       # reserved, unused
        # SkyPilot task fragment, merged under each job's sky.yaml
        "defaults": {"resources": {"cpus": 4, "memory": 16}},
    },
}


def config_path(explicit: Optional[str] = None) -> Path:
    """Resolve the config location: explicit arg > $XR3_CONFIG > default."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("XR3_CONFIG")
    if env:
        return Path(env)
    return DEFAULT_CONFIG_PATH


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base` (dict values only)."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Optional[str] = None) -> dict:
    """Load config merged over DEFAULTS. Missing file -> DEFAULTS."""
    cfg_path = config_path(path)
    if not cfg_path.is_file():
        return _deep_merge(DEFAULTS, {})
    with open(cfg_path) as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(
            f"xr3 config at {cfg_path} must be a mapping, "
            f"got {type(loaded).__name__}"
        )
    return _deep_merge(DEFAULTS, loaded)
