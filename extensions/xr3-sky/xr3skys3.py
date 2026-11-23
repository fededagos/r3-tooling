"""Object store abstraction and push/pull planning for xr3-sky.

Keys are bucket-relative (e.g. ``r3/jobs/<id>/files/run.sh``); the bucket is
fixed per Store instance. stdlib + pyyaml only at import time (boto3 is lazy).
"""
from __future__ import annotations

import functools
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

import yaml

from xr3skylib import S3Layout


class StoreUnreachable(Exception):
    pass


class StoreAuthError(Exception):
    """Credentials missing, incomplete or rejected (e.g. 403)."""


class StoreBucketMissing(Exception):
    """The configured bucket does not exist on the endpoint (404 / NoSuchBucket)."""


class PushError(Exception):
    pass


@dataclass
class ObjectInfo:
    key: str
    size: int
    etag: str
    # S3 LastModified as epoch seconds (None if unknown). Needed for multipart
    # objects, whose etag is not an MD5 of the content.
    last_modified: Optional[float] = None


class Store(Protocol):
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[ObjectInfo]: ...
    def read_text(self, key: str) -> Optional[str]: ...
    def write_text(self, key: str, text: str) -> None: ...
    def upload_file(self, path: Path, key: str) -> None: ...
    def download_file(self, key: str, path: Path) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...


class _Objects(dict):
    """key -> bytes; every assignment stamps `last_modified[key]` with the time."""

    def __init__(self, last_modified: dict) -> None:
        super().__init__()
        self.last_modified = last_modified

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.last_modified[key] = time.time()

    def __delitem__(self, key):
        super().__delitem__(key)
        self.last_modified.pop(key, None)


class MemoryStore:
    """Dict-backed Store for tests; byte-faithful, etag is md5 hex.

    With `multipart_etags`, etags look like multipart uploads (`<hex>-1`, not an
    MD5 of the content). `last_modified` (key -> epoch) is stamped on every write
    and may be set by tests."""

    def __init__(self, multipart_etags: bool = False) -> None:
        self.multipart_etags = multipart_etags
        self.last_modified: dict[str, float] = {}
        self.objects: dict[str, bytes] = _Objects(self.last_modified)

    def _etag(self, data: bytes) -> str:
        if self.multipart_etags:
            return hashlib.md5(hashlib.md5(data).digest()).hexdigest() + "-1"
        return hashlib.md5(data).hexdigest()

    def exists(self, key):
        return key in self.objects

    def list(self, prefix):
        return [ObjectInfo(k, len(v), self._etag(v), self.last_modified.get(k))
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


# head_bucket has no body, so errors often carry only the HTTP status as the code.
_AUTH_CODES = {"403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
               "Forbidden"}
_MISSING_BUCKET_CODES = {"404", "NoSuchBucket", "NotFound"}


def _connection_errors() -> tuple:
    """botocore's connection-class errors: the endpoint (VPN) is not reachable."""
    import botocore.exceptions as be
    return (be.EndpointConnectionError, be.ConnectionClosedError,
            be.ConnectTimeoutError, be.ReadTimeoutError, be.SSLError)


def _connection_cause(exc: BaseException) -> Optional[BaseException]:
    """`exc` or the first exception in its cause/context chain that is a
    connection error (s3transfer wraps them, e.g. in S3UploadFailedError)."""
    errors = _connection_errors()
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, errors):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def _unreachable_on_connection_error(method):
    """Re-raise connection-class errors from a BotoStore operation as StoreUnreachable."""
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except StoreUnreachable:
            raise
        except Exception as e:
            cause = _connection_cause(e)
            if cause is None:
                raise
            raise StoreUnreachable(str(cause)) from e
    return wrapper


def _boto_config(Config):
    """The client Config. Fail fast when the endpoint is unreachable (VPN down):
    well under a minute, instead of botocore's default retries x 60 s timeouts.
    Checksums only when required: botocore >= 1.36 otherwise adds CRC checksum
    headers to every request (delete_objects included), which Ceph RGW may reject;
    older botocore does not know the options and does not send the headers."""
    base = dict(s3={"addressing_style": "path"}, max_pool_connections=32,
                connect_timeout=10, read_timeout=120,
                retries={"max_attempts": 3, "mode": "standard"})
    try:
        return Config(**base, request_checksum_calculation="when_required",
                      response_checksum_validation="when_required")
    except TypeError:
        return Config(**base)


class BotoStore:
    def __init__(self, bucket: str, endpoint: str, profile: Optional[str] = None):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self.client = session.client("s3", endpoint_url=endpoint,
                                     config=_boto_config(Config))

    @_unreachable_on_connection_error
    def ping(self) -> None:
        import botocore.exceptions as be
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (be.NoCredentialsError, be.PartialCredentialsError) as e:
            raise StoreAuthError(str(e)) from e
        except be.ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in _AUTH_CODES:
                raise StoreAuthError(str(e)) from e
            if code in _MISSING_BUCKET_CODES:
                raise StoreBucketMissing(str(e)) from e
            raise

    @_unreachable_on_connection_error
    def exists(self, key):
        import botocore.exceptions as be
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except be.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    @_unreachable_on_connection_error
    def list(self, prefix):
        out = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix):
            out += [ObjectInfo(o["Key"], o["Size"], o["ETag"].strip('"'),
                               o["LastModified"].timestamp()
                               if o.get("LastModified") is not None else None)
                    for o in page.get("Contents", [])]
        return out

    @_unreachable_on_connection_error
    def read_text(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode()
        except self.client.exceptions.NoSuchKey:
            return None

    @_unreachable_on_connection_error
    def write_text(self, key, text):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=text.encode())

    @_unreachable_on_connection_error
    def upload_file(self, path, key):
        self.client.upload_file(str(path), self.bucket, key)

    @_unreachable_on_connection_error
    def download_file(self, key, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(path))

    @_unreachable_on_connection_error
    def delete_prefix(self, prefix):
        keys = [o.key for o in self.list(prefix)]
        deleted = 0
        for i in range(0, len(keys), 1000):
            resp = self.client.delete_objects(Bucket=self.bucket, Delete={
                "Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": False})
            if resp.get("Errors"):
                raise RuntimeError("failed to delete: " + ", ".join(
                    e.get("Key", "?") for e in resp["Errors"]))
            deleted += len(resp.get("Deleted", []))
        return deleted


@dataclass
class DepInfo:
    job_id: str
    local_output: Optional[Path]
    local_done: bool
    has_remote_yaml: bool
    env_marker: bool
    # Committed job dir; its files (minus output/) go to layout.files(job_id) so
    # r3's recursive checkout on the pod can read the dependency's r3.yaml.
    job_dir: Optional[Path] = None


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
    """Yield (path, posix relpath) for files under root.

    File symlinks upload the target's content; broken links and symlinked
    directories raise PushError.
    """
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if exclude_top and rel.parts[0] == exclude_top:
            continue
        if p.is_symlink():
            if not p.exists():
                raise PushError(f"broken symlink: {p}")
            if p.is_dir():
                raise PushError(f"symlinked directory not supported: {p}")
        if p.is_file():
            yield p, rel.as_posix()


def plan_push(job_id: str, job_dir: Path, deps: list[DepInfo],
              git_deps: list[tuple[str, str]], store: Store, layout: S3Layout,
              allow_unfinished: bool, *, repush_deps: bool = False) -> PushPlan:
    """What `launch` must upload for `job_id` and its dependency closure.

    A dependency's output is skipped when S3 has its output.complete marker, unless
    `repush_deps` and the dependency has local output (then it is uploaded again;
    the marker stays). A dependency with a local REMOTE.yaml must already be
    complete in S3. An unfinished dependency (allow_unfinished) is uploaded without
    an output.complete marker, so it is uploaded again on every launch.
    """
    plan = PushPlan([], [], 0, [])
    for d in deps:
        if d.env_marker:
            raise PushError(f"depends on a container/venv environment job {d.job_id}")

    if store.exists(layout.files_complete(job_id)):
        plan.skipped.append(job_id)
    else:
        for p, rel in _tree(Path(job_dir), exclude_top="output"):
            plan.uploads.append((p, layout.files(job_id) + rel))
        plan.complete_markers.append(layout.files_complete(job_id))

    for d in deps:
        if d.job_dir is None or store.exists(layout.files_complete(d.job_id)):
            continue
        for p, rel in _tree(Path(d.job_dir), exclude_top="output"):
            plan.uploads.append((p, layout.files(d.job_id) + rel))
        plan.complete_markers.append(layout.files_complete(d.job_id))

    for d in deps:
        complete = store.exists(layout.complete(d.job_id))
        # r3 creates an empty output/ for every committed job, so "has local output"
        # means the directory holds something, not merely that it exists.
        has_local = (d.local_output is not None and Path(d.local_output).is_dir()
                     and any(Path(d.local_output).iterdir()))
        if d.has_remote_yaml and not complete:
            raise PushError(
                f"dependency {d.job_id} has REMOTE.yaml but no complete output in S3")
        if complete and not (repush_deps and has_local):
            plan.skipped.append(d.job_id)
            continue
        if not has_local:
            raise PushError(f"dependency {d.job_id} has no output locally or in S3")
        if not d.local_done:
            msg = f"dependency {d.job_id} is unfinished (no output/done)"
            if not allow_unfinished:
                raise PushError(msg + "; pass --allow-unfinished-deps to push it anyway")
            plan.warnings.append(
                msg + "; its output is uploaded on every launch (no output.complete)")
        # A local REMOTE.yaml only describes what this laptop did not pull.
        for p, rel in _tree(Path(d.local_output), exclude_top="REMOTE.yaml"):
            plan.uploads.append((p, layout.output(d.job_id) + rel))
        if d.local_done and not complete:
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
    for o in remote:
        if not o.key.startswith(output_prefix):
            raise ValueError(f"key {o.key!r} does not start with {output_prefix!r}")
    return yaml.safe_dump([
        {"path": o.key[len(output_prefix):],
         "size": o.size, "etag": o.etag, "uri": f"s3://{bucket}/{o.key}"}
        for o in remote], sort_keys=False)
