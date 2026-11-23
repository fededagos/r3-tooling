import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "xr3"))

import xr3skylib as lib
import xr3skys3 as s3

LAYOUT = lib.S3Layout("r3")


def make_job(tmp_path):
    job = tmp_path / "job"
    (job / "output").mkdir(parents=True)
    (job / "sub").mkdir()
    (job / "run.sh").write_text("echo hi\n")
    (job / "sub" / "a.txt").write_text("abc")
    (job / "output" / "ignored").write_text("zzz")
    return job


def make_dep(tmp_path, name="dep1", done=True, **kw):
    out = tmp_path / name / "output"
    out.mkdir(parents=True)
    (out / "r.txt").write_text("12345")
    if done:
        (out / "done").write_text("")
    return s3.DepInfo(name, out, done, kw.get("has_remote_yaml", False), kw.get("env_marker", False))


def plan(job, deps, store, git_deps=(), allow=False):
    return s3.plan_push("J", job, deps, list(git_deps), store, LAYOUT, allow)


def test_plan_push_uploads_job_files_except_output(tmp_path):
    job = make_job(tmp_path)
    p = plan(job, [], s3.MemoryStore())
    keys = {k for _, k in p.uploads}
    assert keys == {"r3/jobs/J/files/run.sh", "r3/jobs/J/files/sub/a.txt"}
    assert p.total_bytes == len("echo hi\n") + 3


def test_plan_push_skips_already_uploaded_files_prefix(tmp_path):
    job = make_job(tmp_path)
    store = s3.MemoryStore()
    store.write_text("r3/jobs/J/files/run.sh", "x")
    store.write_text(LAYOUT.files_complete("J"), "")
    p = plan(job, [], store)
    assert p.uploads == []
    assert p.total_bytes == 0
    assert p.skipped == ["J"]
    assert p.complete_markers == []


def test_plan_push_partial_files_upload_is_redone(tmp_path):
    job = make_job(tmp_path)
    store = s3.MemoryStore()
    store.write_text("r3/jobs/J/files/run.sh", "x")
    p = plan(job, [], store)
    assert len(p.uploads) == 2
    assert p.complete_markers == ["r3/jobs/J/files.complete"]


def test_plan_push_symlinks(tmp_path):
    job = make_job(tmp_path)
    (job / "link.txt").symlink_to(job / "run.sh")
    p = plan(job, [], s3.MemoryStore())
    assert "r3/jobs/J/files/link.txt" in {k for _, k in p.uploads}
    (job / "bad").symlink_to(tmp_path / "nope")
    with pytest.raises(s3.PushError, match="bad"):
        plan(job, [], s3.MemoryStore())
    (job / "bad").unlink()
    (job / "dlink").symlink_to(job / "sub")
    with pytest.raises(s3.PushError, match="symlinked directory not supported"):
        plan(job, [], s3.MemoryStore())


def test_plan_push_dep_symlink_policy(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path)
    (dep.local_output / "bad").symlink_to(tmp_path / "nope")
    with pytest.raises(s3.PushError, match="bad"):
        plan(job, [dep], s3.MemoryStore())


def test_plan_push_dep_without_local_output_but_complete_in_s3(tmp_path):
    job = make_job(tmp_path)
    store = s3.MemoryStore()
    store.write_text("r3/jobs/depR/output.complete", "")
    p = plan(job, [s3.DepInfo("depR", None, False, False, False)], store)
    assert "depR" in p.skipped and not any("depR" in k for _, k in p.uploads)


def test_plan_push_existing_bundle_not_missing(tmp_path):
    job = make_job(tmp_path)
    store = s3.MemoryStore()
    store.write_text(LAYOUT.git_bundle("/repo", "abc"), "b")
    assert plan(job, [], store, git_deps=[("/repo", "abc")]).missing_bundles == []


def test_remote_yaml_bad_prefix():
    with pytest.raises(ValueError):
        s3.remote_yaml([s3.ObjectInfo("other/x", 1, "e")], "b", "r3/jobs/J/output/")


def test_plan_push_dep_with_remote_yaml_not_uploaded(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path, has_remote_yaml=True)
    p = plan(job, [dep], s3.MemoryStore())
    assert not any("dep1" in k for _, k in p.uploads)
    assert p.complete_markers == ["r3/jobs/J/files.complete"]
    assert "dep1" in p.skipped


def test_plan_push_uploads_dep_output_with_marker(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path)
    p = plan(job, [dep], s3.MemoryStore(), git_deps=[("/repo", "abc")])
    keys = {k for _, k in p.uploads}
    assert "r3/jobs/dep1/output/r.txt" in keys
    assert "r3/jobs/dep1/output/done" in keys
    assert p.complete_markers == ["r3/jobs/J/files.complete", "r3/jobs/dep1/output.complete"]
    assert p.missing_bundles == [LAYOUT.git_bundle("/repo", "abc")]


def test_plan_push_dep_complete_in_store_skipped(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path)
    store = s3.MemoryStore()
    store.write_text("r3/jobs/dep1/output.complete", "")
    p = plan(job, [dep], store)
    assert p.complete_markers == ["r3/jobs/J/files.complete"]
    assert not any("dep1" in k for _, k in p.uploads)


def test_plan_push_missing_dep_output_errors(tmp_path):
    job = make_job(tmp_path)
    dep = s3.DepInfo("depX", None, False, False, False)
    with pytest.raises(s3.PushError, match="depX"):
        plan(job, [dep], s3.MemoryStore())


def test_plan_push_env_dependency_refused(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path, env_marker=True)
    with pytest.raises(s3.PushError, match="container/venv environment job dep1"):
        plan(job, [dep], s3.MemoryStore())


def test_plan_push_unfinished_dep_warns_or_errors(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path, done=False)
    with pytest.raises(s3.PushError, match="--allow-unfinished-deps"):
        plan(job, [dep], s3.MemoryStore())
    p = plan(job, [dep], s3.MemoryStore(), allow=True)
    assert p.warnings


def test_split_for_pull_threshold():
    objs = [s3.ObjectInfo("a", 10, "e"), s3.ObjectInfo("b", 150_000_000, "f")]
    dl, remote = s3.split_for_pull(objs, 100_000_000)
    assert [o.key for o in dl] == ["a"] and [o.key for o in remote] == ["b"]
    dl, remote = s3.split_for_pull([s3.ObjectInfo("c", 100, "e")], 100)
    assert [o.key for o in dl] == ["c"] and remote == []
    dl, remote = s3.split_for_pull(objs, None)
    assert len(dl) == 2 and remote == []


def test_remote_yaml_roundtrip():
    objs = [s3.ObjectInfo("r3/jobs/J/output/x/y.bin", 5, "abc")]
    data = yaml.safe_load(s3.remote_yaml(objs, "bkt", "r3/jobs/J/output/"))
    assert data == [{"path": "x/y.bin", "size": 5, "etag": "abc",
                     "uri": "s3://bkt/r3/jobs/J/output/x/y.bin"}]


def test_memory_store_byte_faithful(tmp_path):
    import hashlib
    src = tmp_path / "s.bin"
    src.write_bytes(b"\x00\xff\x10")
    st = s3.MemoryStore()
    st.upload_file(src, "p/k")
    dst = tmp_path / "d" / "o.bin"
    st.download_file("p/k", dst)
    assert dst.read_bytes() == b"\x00\xff\x10"
    assert st.list("p/")[0].etag == hashlib.md5(b"\x00\xff\x10").hexdigest()
    assert st.delete_prefix("p/") == 1 and not st.exists("p/k")


def make_dep_with_files(tmp_path, name="dep1", **kw):
    dep = make_dep(tmp_path, name, **kw)
    jd = dep.local_output.parent
    (jd / "r3.yaml").write_text("dependencies: []\n")
    (jd / "run.sh").write_text("echo dep\n")
    dep.job_dir = jd
    return dep


def test_depinfo_job_dir_defaults_to_none():
    assert s3.DepInfo("d", None, False, False, False).job_dir is None


def test_plan_push_uploads_dep_job_files_except_output(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep_with_files(tmp_path)
    p = plan(job, [dep], s3.MemoryStore())
    keys = {k for _, k in p.uploads}
    assert {"r3/jobs/dep1/files/r3.yaml", "r3/jobs/dep1/files/run.sh"} <= keys
    assert not any(k.startswith("r3/jobs/dep1/files/output") for k in keys)
    assert p.complete_markers[0] == "r3/jobs/J/files.complete"
    assert "r3/jobs/dep1/files.complete" in p.complete_markers
    assert "r3/jobs/dep1/output.complete" in p.complete_markers


def test_plan_push_dep_files_skipped_when_files_complete(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep_with_files(tmp_path, has_remote_yaml=True)
    store = s3.MemoryStore()
    store.write_text(LAYOUT.files_complete("dep1"), "")
    p = plan(job, [dep], store)
    assert not any("dep1" in k for _, k in p.uploads)
    assert "r3/jobs/dep1/files.complete" not in p.complete_markers


def test_plan_push_dep_files_pushed_even_if_output_remote(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep_with_files(tmp_path, has_remote_yaml=True)
    p = plan(job, [dep], s3.MemoryStore())
    keys = {k for _, k in p.uploads}
    assert "r3/jobs/dep1/files/r3.yaml" in keys
    assert not any(k.startswith("r3/jobs/dep1/output/") for k in keys)
    assert "r3/jobs/dep1/files.complete" in p.complete_markers


# --- BotoStore.ping error mapping (stub client; botocore needed) ----------------

def _stub_store(exc):
    pytest.importorskip("botocore")

    class Client:
        def head_bucket(self, Bucket):
            raise exc

    store = object.__new__(s3.BotoStore)
    store.bucket = "b"
    store.client = Client()
    return store


def _client_error(code):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "HeadBucket")


@pytest.mark.parametrize("make, expected", [
    (lambda be: be.NoCredentialsError(), "StoreAuthError"),
    (lambda be: be.PartialCredentialsError(provider="p", cred_var="v"), "StoreAuthError"),
    (lambda be: _client_error("403"), "StoreAuthError"),
    (lambda be: _client_error("InvalidAccessKeyId"), "StoreAuthError"),
    (lambda be: _client_error("404"), "StoreBucketMissing"),
    (lambda be: _client_error("NoSuchBucket"), "StoreBucketMissing"),
    (lambda be: be.EndpointConnectionError(endpoint_url="https://x"), "StoreUnreachable"),
])
def test_boto_ping_maps_errors(make, expected):
    be = pytest.importorskip("botocore.exceptions")
    store = _stub_store(make(be))
    with pytest.raises(getattr(s3, expected)):
        store.ping()


def test_boto_ping_reraises_other_client_errors():
    pytest.importorskip("botocore")
    store = _stub_store(_client_error("500"))
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        store.ping()



# --- every BotoStore operation maps connection errors (stub client) -------------

class _DeadClient:
    """Every S3 call fails as if the endpoint (VPN) is gone."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, exc):
        self.exc = exc

    def __getattr__(self, name):
        def call(*a, **k):
            raise self.exc
        return call

    def get_paginator(self, name):
        exc = self.exc

        class P:
            def paginate(self, **k):
                raise exc
        return P()


@pytest.mark.parametrize("make", [
    lambda be: be.EndpointConnectionError(endpoint_url="https://x"),
    lambda be: be.ConnectTimeoutError(endpoint_url="https://x"),
    lambda be: be.ReadTimeoutError(endpoint_url="https://x"),
    lambda be: be.ConnectionClosedError(endpoint_url="https://x"),
    lambda be: be.SSLError(endpoint_url="https://x", error="bad"),
])
@pytest.mark.parametrize("op", [
    lambda st, tmp: st.exists("k"),
    lambda st, tmp: st.list("p/"),
    lambda st, tmp: st.read_text("k"),
    lambda st, tmp: st.write_text("k", "v"),
    lambda st, tmp: st.upload_file(tmp / "f", "k"),
    lambda st, tmp: st.download_file("k", tmp / "d"),
    lambda st, tmp: st.delete_prefix("p/"),
])
def test_boto_ops_map_connection_errors(make, op, tmp_path):
    be = pytest.importorskip("botocore.exceptions")
    store = object.__new__(s3.BotoStore)
    store.bucket = "b"
    store.client = _DeadClient(make(be))
    (tmp_path / "f").write_text("x")
    with pytest.raises(s3.StoreUnreachable):
        op(store, tmp_path)


def test_boto_upload_failure_wrapping_connection_error_is_unreachable(tmp_path):
    be = pytest.importorskip("botocore.exceptions")
    bex = pytest.importorskip("boto3.exceptions")
    try:
        try:
            raise be.EndpointConnectionError(endpoint_url="https://x")
        except Exception as inner:
            raise bex.S3UploadFailedError("Failed to upload") from inner
    except bex.S3UploadFailedError as e:
        wrapped = e
    store = object.__new__(s3.BotoStore)
    store.bucket = "b"
    store.client = _DeadClient(wrapped)
    (tmp_path / "f").write_text("x")
    with pytest.raises(s3.StoreUnreachable):
        store.upload_file(tmp_path / "f", "k")


def test_boto_ops_keep_other_errors():
    be = pytest.importorskip("botocore.exceptions")
    store = object.__new__(s3.BotoStore)
    store.bucket = "b"
    store.client = _DeadClient(_client_error("500"))
    with pytest.raises(be.ClientError):
        store.exists("k")


def test_boto_client_fails_fast():
    pytest.importorskip("boto3")
    store = s3.BotoStore("b", "https://s3.invalid.example", None)
    cfg = store.client.meta.config
    assert cfg.connect_timeout == 10 and cfg.read_timeout == 120
    # botocore normalises retries={"max_attempts": 3} to 4 attempts in total:
    # worst case ~4 x 10 s connect timeouts plus backoff, well under a minute.
    assert cfg.retries == {"total_max_attempts": 4, "mode": "standard"}
    assert cfg.max_pool_connections == 32
    assert cfg.s3 == {"addressing_style": "path"}
