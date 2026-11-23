"""Object store abstraction and push/pull planning for xr3-sky.

Keys are bucket-relative (e.g. ``r3/jobs/<id>/files/run.sh``); the bucket is
fixed per Store instance. stdlib + pyyaml only at import time (boto3 is lazy).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

import yaml

from xr3skylib import S3Layout


class StoreUnreachable(Exception):
    pass


class PushError(Exception):
    pass


@dataclass
class ObjectInfo:
    key: str
    size: int
    etag: str


class Store(Protocol):
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[ObjectInfo]: ...
    def read_text(self, key: str) -> Optional[str]: ...
    def write_text(self, key: str, text: str) -> None: ...
    def upload_file(self, path: Path, key: str) -> None: ...
    def download_file(self, key: str, path: Path) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...


class MemoryStore:
    """Dict-backed Store for tests; byte-faithful, etag is md5 hex."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def exists(self, key):
        return key in self.objects

    def list(self, prefix):
        return [ObjectInfo(k, len(v), hashlib.md5(v).hexdigest())
                for k, v in sorted(self.objects.items()) if k.startswith(prefix)]

    def read_text(self, key):
        v = self.objects.get(key)
        return None if v is None else v.decode()

    def write_text(self, key, text):
        self.objects[key] = text.encode()

    def upload_file(self, path, key):
        self.objects[key] = Path(path).read_bytes()

    def download_file(self, key, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[key])

    def delete_prefix(self, prefix):
        keys = [k for k in self.objects if k.startswith(prefix)]
        for k in keys:
            del self.objects[k]
        return len(keys)


class BotoStore:
    def __init__(self, bucket: str, endpoint: str, profile: Optional[str] = None):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self.client = session.client(
            "s3", endpoint_url=endpoint,
            config=Config(s3={"addressing_style": "path"}, max_pool_connections=32))

    def ping(self) -> None:
        import botocore.exceptions as be
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (be.EndpointConnectionError, be.ConnectionClosedError,
                be.ConnectTimeoutError, be.ReadTimeoutError, be.SSLError) as e:
            raise StoreUnreachable(str(e)) from e

    def exists(self, key):
        import botocore.exceptions as be
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except be.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list(self, prefix):
        out = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix):
            out += [ObjectInfo(o["Key"], o["Size"], o["ETag"].strip('"'))
                    for o in page.get("Contents", [])]
        return out

    def read_text(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode()
        except self.client.exceptions.NoSuchKey:
            return None

    def write_text(self, key, text):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=text.encode())

    def upload_file(self, path, key):
        self.client.upload_file(str(path), self.bucket, key)

    def download_file(self, key, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(path))

    def delete_prefix(self, prefix):
        keys = [o.key for o in self.list(prefix)]
        for i in range(0, len(keys), 1000):
            self.client.delete_objects(Bucket=self.bucket, Delete={
                "Objects": [{"Key": k} for k in keys[i:i + 1000]]})
        return len(keys)


@dataclass
class DepInfo:
    job_id: str
    local_output: Optional[Path]
    local_done: bool
    has_remote_yaml: bool
    env_marker: bool


@dataclass
class PushPlan:
    uploads: list[tuple[Path, str]]
    skipped: list[str]
    total_bytes: int
    warnings: list[str]
    # Marker keys (layout.complete(dep)) to write AFTER all `uploads` succeed;
    # write each with store.write_text(key, ""). Not part of `uploads`.
    complete_markers: list[str] = field(default_factory=list)
    # Bundle keys not yet in the store; the CLI creates and uploads them.
    missing_bundles: list[str] = field(default_factory=list)


def _tree(root: Path, exclude_top: Optional[str] = None):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if exclude_top and rel.parts[0] == exclude_top:
            continue
        yield p, rel.as_posix()


def plan_push(job_id: str, job_dir: Path, deps: list[DepInfo],
              git_deps: list[tuple[str, str]], store: Store, layout: S3Layout,
              allow_unfinished: bool) -> PushPlan:
    plan = PushPlan([], [], 0, [])
    for d in deps:
        if d.env_marker:
            raise PushError(f"depends on a container/venv environment job {d.job_id}")

    if store.list(layout.files(job_id)):
        plan.skipped.append(job_id)
    else:
        for p, rel in _tree(Path(job_dir), exclude_top="output"):
            plan.uploads.append((p, layout.files(job_id) + rel))

    for d in deps:
        if d.has_remote_yaml or store.exists(layout.complete(d.job_id)):
            plan.skipped.append(d.job_id)
            continue
        if d.local_output is None or not Path(d.local_output).is_dir():
            raise PushError(f"dependency {d.job_id} has no output locally or in S3")
        if not d.local_done:
            msg = f"dependency {d.job_id} is unfinished (no output/done)"
            if not allow_unfinished:
                raise PushError(msg + "; pass --allow-unfinished-deps to push it anyway")
            plan.warnings.append(msg)
        for p, rel in _tree(Path(d.local_output)):
            plan.uploads.append((p, layout.output(d.job_id) + rel))
        plan.complete_markers.append(layout.complete(d.job_id))

    for repo, commit in git_deps:
        key = layout.git_bundle(repo, commit)
        if not store.exists(key):
            plan.missing_bundles.append(key)

    plan.total_bytes = sum(p.stat().st_size for p, _ in plan.uploads)
    return plan


def split_for_pull(objects: list[ObjectInfo], max_bytes: Optional[int]
                   ) -> tuple[list[ObjectInfo], list[ObjectInfo]]:
    if max_bytes is None:
        return list(objects), []
    return ([o for o in objects if o.size <= max_bytes],
            [o for o in objects if o.size > max_bytes])


def remote_yaml(remote: list[ObjectInfo], bucket: str, output_prefix: str) -> str:
    return yaml.safe_dump([
        {"path": o.key[len(output_prefix):] if o.key.startswith(output_prefix) else o.key,
         "size": o.size, "etag": o.etag, "uri": f"s3://{bucket}/{o.key}"}
        for o in remote], sort_keys=False)
