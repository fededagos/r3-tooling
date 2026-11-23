"""Pure helpers for xr3-sky (stdlib only; unit-testable without sky/r3/boto3).

Sections: sizes, identity & naming, S3 key layout.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping, Optional

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
