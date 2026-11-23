"""Pure helpers for xr3-sky (stdlib only; unit-testable without sky/r3/boto3).

Sections: sizes, identity & naming, S3 key layout, done state & launch decision,
sky.yaml merge & validation.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Literal, Mapping, Optional

# --- sizes -------------------------------------------------------------------

_SIZE_UNITS = {"": 1, "B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}


def parse_size(text: str) -> int:
    """Parse "100MB" / "5GB" / "1234" into bytes (decimal units)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", str(text))
    unit = m.group(2).upper() if m else None
    if not m or unit not in _SIZE_UNITS:
        raise ValueError(f"invalid size: {text!r}")
    return int(float(m.group(1)) * _SIZE_UNITS[unit])


# --- identity & naming -------------------------------------------------------

def sky_user(cfg: Mapping, env: Mapping[str, str]) -> str:
    """User name: `sky.user` config, else local part of $SKYPILOT_USER."""
    user = cfg.get("user")
    if user:
        return user
    sp = env.get("SKYPILOT_USER")
    if sp:
        return sp.split("@", 1)[0]
    raise ValueError("set sky.user or SKYPILOT_USER")


def managed_job_name(user: str, job_id: str) -> str:
    """SkyPilot managed-job name: `<user>-r3-<first 8 chars of job id>`."""
    return f"{user}-r3-{job_id[:8]}"


def job_id_prefix_from_name(name: str, user: str) -> Optional[str]:
    """Inverse of managed_job_name: the 8-char job-id prefix, or None."""
    m = re.fullmatch(re.escape(user) + r"-r3-([0-9a-f]{8})", name)
    return m.group(1) if m else None


# --- S3 key layout -----------------------------------------------------------

@dataclass(frozen=True)
class S3Layout:
    prefix: str

    def _job(self, job_id: str) -> str:
        return f"{self.prefix}/jobs/{job_id}"

    def files(self, job_id: str) -> str:
        return f"{self._job(job_id)}/files/"

    def output(self, job_id: str) -> str:
        return f"{self._job(job_id)}/output/"

    def done(self, job_id: str) -> str:
        return f"{self._job(job_id)}/output/done"

    def complete(self, job_id: str) -> str:
        return f"{self._job(job_id)}/output.complete"

    def git_bundle(self, repository_path: str, commit: str) -> str:
        digest = hashlib.sha256(repository_path.encode()).hexdigest()
        return f"{self.prefix}/git/{digest}/{commit}.bundle"

    def run_record(self, job_id: str, ts: str) -> str:
        return f"{self.prefix}/runs/{job_id}/{ts}/task.yaml"


# --- done state & launch decision --------------------------------------------

def _classify_done(text: str) -> str:
    """Classify an `output/done` body; matches xr3slurmlib.parse_done_state."""
    body = text.strip()
    if body == "restart":
        return "restart"
    if body == "failed":
        return "failed"
    return "completed"


def effective_done(s3_text: Optional[str], local_text: Optional[str]) -> Optional[str]:
    """Done state from S3 if its marker exists, else local; None if neither.

    An empty body is a present marker and counts as "completed".
    """
    if s3_text is not None:
        return _classify_done(s3_text)
    if local_text is not None:
        return _classify_done(local_text)
    return None


def launch_decision(state: Optional[str], restart_failed: bool
                    ) -> Literal["skip", "launch", "launch_rewrite_restart"]:
    """Whether to (re)launch a job given its done state."""
    if state is None or state == "restart":
        return "launch"
    if state == "failed":
        return "launch_rewrite_restart" if restart_failed else "skip"
    return "skip"


# --- sky.yaml merge & validation ---------------------------------------------

class SkySpecError(ValueError):
    """A job's sky.yaml (merged with defaults) is not safe to submit."""


# gpumem in MiB: (one slot, whole card)
HAMI_GPUMEM: dict[str, tuple[int, int]] = {
    "RTXPRO6000": (9788, 97887),
    "H100-80GB": (8155, 81559),
    "L40S": (4606, 46068),
}

_JOB_KEYS = {"resources", "config", "envs", "secrets", "num_nodes"}
_PRIORITY_CLASSES = ("low", "medium", "high")
_GPUMEM_KEY = "nvidia.com/gpumem"


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def merge_task(defaults: dict, job_fragment: dict) -> dict:
    """Deep-merge a job's sky.yaml fragment over the defaults (job wins)."""
    bad = sorted(set(job_fragment) - _JOB_KEYS)
    if bad:
        raise SkySpecError(
            f"sky.yaml may only set {sorted(_JOB_KEYS)}; not allowed: {bad}")
    return _deep_merge(defaults, job_fragment)


def _acc_name(acc) -> Optional[str]:
    """Name of a single-accelerator spec (string or one-key dict), else None."""
    if isinstance(acc, str):
        return acc.split(":", 1)[0]
    if isinstance(acc, dict) and len(acc) == 1:
        return str(next(iter(acc)))
    return None


def accelerator_of(task: dict) -> Optional[str]:
    """The single pinned accelerator name, or None (no GPU / not single)."""
    return _acc_name((task.get("resources") or {}).get("accelerators"))


def _containers(task: dict) -> Optional[list]:
    try:
        c = task["config"]["kubernetes"]["pod_config"]["spec"]["containers"]
    except (KeyError, TypeError):
        return None
    return c if isinstance(c, list) and c else None


def gpumem_of(task: dict) -> Optional[int]:
    """The nvidia.com/gpumem limit of container 0, or None."""
    c = _containers(task)
    try:
        v = c[0]["resources"]["limits"][_GPUMEM_KEY]
    except (KeyError, TypeError):
        return None
    return int(v)


def _set_gpumem(task: dict, value: int) -> None:
    node = task
    for key in ("config", "kubernetes", "pod_config", "spec"):
        node = node.setdefault(key, {})
    containers = node.setdefault("containers", [])
    if not containers:
        containers.append({})
    limits = containers[0].setdefault("resources", {}).setdefault("limits", {})
    limits[_GPUMEM_KEY] = value


def _has_gpu(res: dict) -> bool:
    return bool(res.get("accelerators"))


def validate_and_fill(task: dict, *, whole_card: bool, allow_high: bool,
                      infra_override: Optional[str]) -> dict:
    """Check the placement rules; return a new task with gpumem/infra filled."""
    task = copy.deepcopy(task)
    if task.get("resources") is None:
        task["resources"] = {}
    res = task["resources"]

    if "config" in res:
        raise SkySpecError("resources.config: must be top-level (use `config:`)")
    if task.get("num_nodes", 1) != 1:
        raise SkySpecError("num_nodes must be 1")

    gpu_in_choices = any(
        _has_gpu(r) for key in ("any_of", "ordered") for r in (res.get(key) or []))
    acc = res.get("accelerators")
    if gpu_in_choices or isinstance(acc, (list, set, tuple, frozenset)) or (
            acc and _acc_name(acc) is None):
        raise SkySpecError("GPU jobs must pin exactly one accelerator")

    name = _acc_name(acc) if acc else None
    if name in HAMI_GPUMEM:
        slot, card = HAMI_GPUMEM[name]
        mem = gpumem_of(task)
        if mem is None:
            if not whole_card:
                raise SkySpecError(
                    f"{name} needs a nvidia.com/gpumem cap: one slot is {slot} MiB, "
                    f"whole card is {card} MiB (pass --whole-card for the whole card)")
            _set_gpumem(task, card)
        elif mem > card:
            raise SkySpecError(
                f"gpumem {mem} exceeds the whole-card value {card} MiB for {name}")

    try:
        prio = task["config"]["kubernetes"]["pod_config"]["spec"].get("priorityClassName")
    except (KeyError, TypeError, AttributeError):
        prio = None
    if prio is not None:
        if prio not in _PRIORITY_CLASSES:
            raise SkySpecError(
                f"priority class {prio!r} not allowed; use one of {list(_PRIORITY_CLASSES)}")
        if prio == "high" and not allow_high:
            raise SkySpecError("priority class 'high' requires --allow-high")

    for mount, vol in (task.get("volumes") or {}).items():
        if str(vol).startswith("do-"):
            raise SkySpecError(
                f"DO has no persistent volume: {mount} uses {vol!r}")

    if infra_override:
        current = res.get("infra")
        if current and current != infra_override:
            raise SkySpecError(
                f"--infra contradicts sky.yaml: {infra_override!r} vs {current!r}")
        res["infra"] = infra_override
    return task
