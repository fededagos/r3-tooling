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
    p = plan(job, [], store)
    assert p.uploads == []
    assert p.total_bytes == 0
    assert p.skipped


def test_plan_push_dep_with_remote_yaml_not_uploaded(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path, has_remote_yaml=True)
    p = plan(job, [dep], s3.MemoryStore())
    assert not any("dep1" in k for _, k in p.uploads)
    assert p.complete_markers == []
    assert "dep1" in p.skipped


def test_plan_push_uploads_dep_output_with_marker(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path)
    p = plan(job, [dep], s3.MemoryStore(), git_deps=[("/repo", "abc")])
    keys = {k for _, k in p.uploads}
    assert "r3/jobs/dep1/output/r.txt" in keys
    assert "r3/jobs/dep1/output/done" in keys
    assert p.complete_markers == ["r3/jobs/dep1/output.complete"]
    assert p.missing_bundles == [LAYOUT.git_bundle("/repo", "abc")]


def test_plan_push_dep_complete_in_store_skipped(tmp_path):
    job = make_job(tmp_path)
    dep = make_dep(tmp_path)
    store = s3.MemoryStore()
    store.write_text("r3/jobs/dep1/output.complete", "")
    p = plan(job, [dep], store)
    assert p.complete_markers == []
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
