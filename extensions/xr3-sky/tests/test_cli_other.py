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


def seed_output(env, job_id, files, done=""):
    """Put `files` (relpath -> bytes) and a done marker under the job's S3 output/."""
    store = env["store"]
    for rel, data in files.items():
        store.objects[LAYOUT.output(job_id) + rel] = data
    if done is not None:
        store.objects[LAYOUT.done(job_id)] = done.encode()


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
