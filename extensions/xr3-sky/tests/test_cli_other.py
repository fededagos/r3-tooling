"""CLI tests for `xr3-sky pull|status|logs|cancel`."""
import json
import stat

import pytest

pytest.importorskip("r3")
pytest.importorskip("click")
from click.testing import CliRunner  # noqa: E402

from cli_support import LAYOUT, _commit, cli_mod, r3, s3, yaml  # noqa: E402


def run(*args):
    return CliRunner().invoke(cli_mod.cli, list(args), catch_exceptions=False)


def calls(log):
    return [json.loads(p.read_text()) for p in sorted(log.glob("call*.json"))]


def seed_output(env, job_id, files, done="", complete=None):
    """Put `files` (relpath -> bytes) and a done marker under the job's S3 output/.

    `complete` (default: whether `done` is a completed marker) also writes
    output.complete, as the pod does after the final sync."""
    store = env["store"]
    for rel, data in files.items():
        store.objects[LAYOUT.output(job_id) + rel] = data
    if done is not None:
        store.objects[LAYOUT.done(job_id)] = done.encode()
    if complete is None:
        complete = done is not None and done.strip() not in ("restart", "failed")
    if complete:
        store.objects[LAYOUT.complete(job_id)] = b""


def out_dir(env, job_id):
    return env["repo"] / "jobs" / job_id / "output"


def test_pull_keeps_large_files_remote_and_writes_remote_yaml(env):
    seed_output(env, env["main"], {"small.txt": b"hi", "sub/big.bin": b"x" * 5000})
    r = run("pull", env["main"], "--max-size", "1KB")
    assert r.exit_code == 0, r.output
    out = out_dir(env, env["main"])
    assert (out / "small.txt").read_bytes() == b"hi"
    assert (out / "done").is_file()
    assert not (out / "sub" / "big.bin").exists()
    remote = cli_mod.yaml.safe_load((out / "REMOTE.yaml").read_text())
    assert [e["path"] for e in remote] == ["sub/big.bin"]
    assert remote[0]["size"] == 5000
    assert remote[0]["uri"].endswith("/" + LAYOUT.output(env["main"]) + "sub/big.bin")
    assert "1 remote" in r.output


def test_pull_all_downloads_everything_and_removes_remote_yaml(env):
    seed_output(env, env["main"], {"small.txt": b"hi", "big.bin": b"x" * 5000})
    run("pull", env["main"], "--max-size", "1KB")
    assert (out_dir(env, env["main"]) / "REMOTE.yaml").exists()
    r = run("pull", env["main"], "--all")
    assert r.exit_code == 0, r.output
    out = out_dir(env, env["main"])
    assert (out / "big.bin").read_bytes() == b"x" * 5000
    assert not (out / "REMOTE.yaml").exists()


def test_pull_is_idempotent_and_repairs_changed_files(env):
    seed_output(env, env["main"], {"a.txt": b"aaa"})
    r1 = run("pull", env["main"])
    assert "downloaded 2" in r1.output  # a.txt + done
    out = out_dir(env, env["main"])
    mtime = (out / "a.txt").stat().st_mtime_ns
    r2 = run("pull", env["main"])
    assert "downloaded 0" in r2.output and "skipped 2" in r2.output
    assert (out / "a.txt").stat().st_mtime_ns == mtime
    (out / "a.txt").write_bytes(b"zzz")  # same size, different content
    r3 = run("pull", env["main"])
    assert "downloaded 1" in r3.output
    assert (out / "a.txt").read_bytes() == b"aaa"


def test_pull_without_output_or_done(env):
    r = run("pull", env["main"])
    assert r.exit_code == 0 and f"no S3 output for {env['main']}" in r.output
    seed_output(env, env["main"], {"partial.txt": b"p"}, done=None)
    r = run("pull", env["main"])
    assert r.exit_code == 0 and "in progress" in r.output
    assert not (out_dir(env, env["main"]) / "partial.txt").exists()


def test_pull_works_when_output_dir_is_readonly(env):
    seed_output(env, env["main"], {"a.txt": b"a"})
    out = out_dir(env, env["main"])
    out.chmod(stat.S_IRUSR | stat.S_IXUSR)
    r = run("pull", env["main"])
    assert r.exit_code == 0, r.output
    assert (out / "a.txt").read_bytes() == b"a"


def _queue(monkeypatch, *jobs):
    monkeypatch.setenv("FAKE_SKY_QUEUE", json.dumps(list(jobs)))


def _mj(name, jid, status="RUNNING", at=1000):
    return {"job_id": jid, "job_name": name, "status": status, "submitted_at": at,
            "end_at": None}


def test_status_degrades_without_s3(env, monkeypatch):
    def down():
        raise s3.StoreUnreachable("no vpn")
    env["store"].ping = down
    name = f"tester-r3-{env['main'][:8]}"
    _queue(monkeypatch, _mj(name, 42))
    r = run("status")
    assert r.exit_code == 0, r.output
    assert "(S3 unreachable — done column from local markers only)" in r.output
    assert env["main"] in r.output and "RUNNING" in r.output and "42" in r.output
    # local markers still show up: base has a local (empty) done marker
    base_line = next(l for l in r.output.splitlines() if env["base"] in l)
    assert "completed" in base_line


def _tagged_job(env, tmp_path, tag):
    src = tmp_path / ("src_" + tag)
    jid = _commit(r3.Repository(str(env["repo"])), src, {
        "run.sh": f"echo {tag}\n", "metadata.yaml": yaml.safe_dump({"tags": [tag]})})
    return jid


def test_status_glob_and_newest_managed_and_sky_failure(env, monkeypatch, tmp_path):
    a = _tagged_job(env, tmp_path, "exp-alpha")
    _tagged_job(env, tmp_path, "other-beta")
    name = f"tester-r3-{a[:8]}"
    _queue(monkeypatch, _mj(name, 1, "FAILED", at=10), _mj(name, 2, "RUNNING", at=20))
    r = run("status", "exp-*")
    assert r.exit_code == 0, r.output
    line = next(l for l in r.output.splitlines() if a in l)
    assert "RUNNING" in line and "FAILED" not in line and "exp-alpha" in line
    assert "other-beta" not in r.output
    # sky failing -> '?' and a note
    monkeypatch.setenv("XR3SKY_SKY", "/nonexistent-sky")
    r = run("status", "exp-*")
    assert r.exit_code == 0, r.output
    assert "?" in next(l for l in r.output.splitlines() if a in l)
    assert "SkyPilot state unavailable" in r.output


def test_logs_resolves_newest_managed_job(env, monkeypatch):
    name = f"tester-r3-{env['main'][:8]}"
    _queue(monkeypatch, _mj(name, 5, "SUCCEEDED", at=100), _mj(name, 9, "RUNNING", at=200),
           _mj("someone-r3-deadbeef", 99, at=300))
    r = run("logs", env["main"])
    assert r.exit_code == 0, r.output
    assert calls(env["log"])[-1]["argv"] == ["jobs", "logs", "9"]
    r = run("logs", env["main"], "--controller")
    assert calls(env["log"])[-1]["argv"] == ["jobs", "logs", "--controller", "9"]


def test_cancel_resolves_newest_and_propagates_exit_code(env, monkeypatch):
    name = f"tester-r3-{env['main'][:8]}"
    _queue(monkeypatch, _mj(name, 5, at=100), _mj(name, 9, at=200))
    monkeypatch.setenv("FAKE_SKY_RC", "3")
    r = run("cancel", env["main"])
    assert r.exit_code == 3
    assert calls(env["log"])[-1]["argv"] == ["jobs", "cancel", "-y", "9"]


def test_logs_and_cancel_without_managed_job(env, monkeypatch):
    _queue(monkeypatch)
    for cmd in ("logs", "cancel"):
        r = run(cmd, env["main"])
        assert r.exit_code != 0
        assert f"no managed job named tester-r3-{env['main'][:8]}" in r.output


def test_pull_downloads_done_last(env, monkeypatch):
    seed_output(env, env["main"], {"a.txt": b"a", "z.txt": b"z"})
    store = env["store"]
    orig = store.download_file

    def flaky(key, path):
        if key.endswith("z.txt"):
            raise OSError("connection lost")
        orig(key, path)
    monkeypatch.setattr(store, "download_file", flaky)
    r = CliRunner().invoke(cli_mod.cli, ["pull", env["main"]])
    assert r.exit_code != 0
    assert not (out_dir(env, env["main"]) / "done").exists()


def test_pull_rejects_unsafe_keys(env):
    prefix = LAYOUT.output(env["main"])
    seed_output(env, env["main"], {"ok.txt": b"ok"})
    env["store"].objects[prefix + "../../r3.yaml"] = b"evil"
    env["store"].objects[prefix + "/etc/x"] = b"evil"
    r3yaml = env["repo"] / "jobs" / env["main"] / "r3.yaml"
    before = r3yaml.read_bytes()
    r = run("pull", env["main"])
    assert r.exit_code == 0, r.output
    assert r.output.count("WARNING: skipping unsafe S3 key") == 2
    assert r3yaml.read_bytes() == before
    assert (out_dir(env, env["main"]) / "ok.txt").read_bytes() == b"ok"


def test_pull_skips_remote_yaml_key_and_unknown_job(env):
    seed_output(env, env["main"], {"REMOTE.yaml": b"theirs", "a.txt": b"a"})
    run("pull", env["main"])
    assert not (out_dir(env, env["main"]) / "REMOTE.yaml").exists()
    r = run("pull", "00000000-0000-0000-0000-000000000000")
    assert r.exit_code != 0
    assert "not in local repository" in r.output and "Traceback" not in r.output


def test_store_without_boto3_is_clean_error(monkeypatch):
    def boom(*a, **k):
        raise ImportError("no boto3")
    monkeypatch.setattr(cli_mod.s3, "BotoStore", boom)
    with pytest.raises(cli_mod.click.ClickException, match="boto3 is not installed"):
        cli_mod._store({"bucket": "b", "endpoint": "e", "aws_profile": None})


def test_status_read_error_degrades(env, monkeypatch):
    def bad(key):
        raise RuntimeError("500")
    monkeypatch.setattr(env["store"], "read_text", bad)
    r = run("status")
    assert r.exit_code == 0, r.output
    assert "S3 unreachable" in r.output


CRED_MSG = ("S3 credentials missing or invalid for profile 'enigma' "
            "— see the README's setup section")


@pytest.mark.parametrize("cmd", [["launch", "--dry"], ["pull"], ["status"]])
@pytest.mark.parametrize("exc, msg", [
    (s3.StoreAuthError("403"), CRED_MSG),
    (s3.StoreBucketMissing("404"),
     "bucket 'federico-r3' not found on https://rgw-cold.enigmatic.stanford.edu"),
])
def test_ping_auth_and_bucket_errors_are_clean(env, cmd, exc, msg):
    def bad():
        raise exc
    env["store"].ping = bad
    args = cmd + ([env["main"]] if cmd[0] != "status" else [])
    r = run(*args)
    assert r.exit_code != 0
    assert msg in r.output
    assert "Traceback" not in r.output


def test_store_missing_profile_is_clean_error(monkeypatch):
    class ProfileNotFound(Exception):
        pass

    def boom(*a, **k):
        raise ProfileNotFound("enigma")
    monkeypatch.setattr(cli_mod.s3, "BotoStore", boom)
    with pytest.raises(cli_mod.click.ClickException, match="credentials missing or invalid"):
        cli_mod._store({"bucket": "b", "endpoint": "e", "aws_profile": "enigma"})


def test_pull_vpn_drop_is_clean_error(env):
    def unreachable(*a, **k):
        raise s3.StoreUnreachable("Could not connect to the endpoint URL")
    env["store"].list = unreachable
    r = CliRunner().invoke(cli_mod.cli, ["pull", env["main"]])
    assert r.exit_code == 1
    assert "S3 unreachable — connect to the Stanford VPN" in r.output
    assert "Traceback" not in r.output
    assert isinstance(r.exception, SystemExit)


# --- fix wave -------------------------------------------------------------------

def test_pull_skips_job_being_rerun(env):
    seed_output(env, env["main"], {"a.txt": b"a"}, done="restart\n")
    r = run("pull", env["main"])
    assert r.exit_code == 0, r.output
    assert f"{env['main']}: in progress (restart), skipping" in r.output
    assert not (out_dir(env, env["main"]) / "a.txt").exists()


def test_pull_skips_completed_without_output_complete(env):
    seed_output(env, env["main"], {"a.txt": b"a"}, done="completed\n", complete=False)
    r = run("pull", env["main"])
    assert r.exit_code == 0, r.output
    assert f"{env['main']}: in progress, skipping" in r.output
    assert not (out_dir(env, env["main"]) / "a.txt").exists()
    assert not (out_dir(env, env["main"]) / "done").exists()


def test_pull_failed_job_is_pullable(env):
    seed_output(env, env["main"], {"partial.txt": b"p"}, done="failed\n")
    r = run("pull", env["main"])
    assert r.exit_code == 0, r.output
    out = out_dir(env, env["main"])
    assert (out / "partial.txt").read_bytes() == b"p"
    assert (out / "done").read_text() == "failed\n"


def test_pull_reads_done_before_listing(env, monkeypatch):
    """A job finishing between list and done-read must not leave a local done with
    files missing: done is read (and classified) before the listing."""
    seed_output(env, env["main"], {"a.txt": b"a"}, done="restart\n")
    store = env["store"]
    orig_list = store.list

    def list_then_finish(prefix):
        objs = orig_list(prefix)
        seed_output(env, env["main"], {"late.txt": b"late"})
        return objs
    monkeypatch.setattr(store, "list", list_then_finish)
    run("pull", env["main"])
    out = out_dir(env, env["main"])
    assert not (out / "done").exists() or (out / "late.txt").exists()


def test_pull_multipart_etag_rewritten_same_size_is_redownloaded(env):
    store = env["store"]
    store.multipart_etags = True
    seed_output(env, env["main"], {"ckpt.bin": b"A" * 64})
    key = LAYOUT.output(env["main"]) + "ckpt.bin"
    store.last_modified[key] = 1_700_000_000.0
    r = run("pull", env["main"])
    assert "downloaded 2" in r.output, r.output
    out = out_dir(env, env["main"])
    assert (out / "ckpt.bin").stat().st_mtime == 1_700_000_000.0
    r = run("pull", env["main"])
    assert "downloaded 0" in r.output, r.output
    store.objects[key] = b"B" * 64                  # same size, rewritten later
    store.last_modified[key] = 1_700_000_500.0
    r = run("pull", env["main"])
    assert "downloaded 1" in r.output, r.output
    assert (out / "ckpt.bin").read_bytes() == b"B" * 64


def test_pull_writes_remote_yaml_before_done(env, monkeypatch):
    seed_output(env, env["main"], {"small.txt": b"hi", "big.bin": b"x" * 5000})
    store = env["store"]
    orig = store.download_file
    seen = {}

    def spy(key, path):
        if key == LAYOUT.done(env["main"]):
            seen["remote_yaml"] = (out_dir(env, env["main"]) / "REMOTE.yaml").exists()
        orig(key, path)
    monkeypatch.setattr(store, "download_file", spy)
    r = run("pull", env["main"], "--max-size", "1KB")
    assert r.exit_code == 0, r.output
    assert seen == {"remote_yaml": True}


def test_cancel_prints_the_managed_job_first(env, monkeypatch):
    name = f"tester-r3-{env['main'][:8]}"
    _queue(monkeypatch, _mj(name, 9, at=200))
    r = run("cancel", env["main"])
    assert r.exit_code == 0, r.output
    assert f"cancelling managed job 9 ({name})" in r.output


@pytest.mark.parametrize("prefix", ["/r3", "r3/", "a/../b", ""])
def test_bad_sky_prefix_is_clean_error(env, monkeypatch, tmp_path, prefix):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(yaml.safe_dump({"sky": {"user": "tester", "prefix": prefix}}))
    monkeypatch.setenv("XR3_CONFIG", str(cfg))
    cli_mod._config.cache_clear()
    r = CliRunner().invoke(cli_mod.cli, ["pull", env["main"]])
    assert r.exit_code != 0
    assert "sky.prefix" in r.output and "Traceback" not in r.output
