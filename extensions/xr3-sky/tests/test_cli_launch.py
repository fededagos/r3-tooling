"""CLI tests for `xr3-sky launch` (needs r3 + click: run under the toolchain venv)."""
import json

import pytest

pytest.importorskip("r3")
pytest.importorskip("click")
from click.testing import CliRunner  # noqa: E402

from cli_support import HERE, LAYOUT, SENTINEL, cli_mod, s3, lib, _git, _commit, r3, yaml  # noqa: E402,F401

def launch(*args):
    return CliRunner().invoke(cli_mod.cli, ["launch", *args], catch_exceptions=False)


def calls(log):
    if not log.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(log.glob("call*.json"))]


def test_launch_dry_prints_yaml_and_writes_nothing(env):
    r = launch(env["main"], "--dry")
    assert r.exit_code == 0, r.output
    assert f"name: tester-r3-{env['main'][:8]}" in r.output
    assert LAYOUT.files(env["main"]) in r.output  # upload plan listed
    assert env["store"].objects == {}
    assert calls(env["log"]) == []


def test_launch_submits_with_secrets_by_name_only(env):
    r = launch(env["main"], "--yes")
    assert r.exit_code == 0, r.output
    (c,) = calls(env["log"])
    argv = c["argv"]
    assert argv[:5] == ["jobs", "launch", "-y", "-n", f"tester-r3-{env['main'][:8]}"]
    assert "--secret" in argv and "AWS_ACCESS_KEY_ID" in argv
    assert "AWS_SECRET_ACCESS_KEY" in argv
    assert not any(SENTINEL in a for a in argv)
    assert c["aws_key_ok"]  # value reached sky via env
    text = (env["log"] / "call0.yaml").read_text()
    assert SENTINEL not in text
    task = yaml.safe_load(text)
    assert task["envs"]["R3_JOB_ID"] == env["main"]
    assert "\nrun: |" in text  # readable block, round-trips exactly
    assert task["run"] == (HERE.parent / "xr3sky_bootstrap.sh").read_text()
    # run record holds the same task
    rec = [k for k in env["store"].objects if k.startswith(f"r3/runs/{env['main']}/")]
    assert len(rec) == 1 and rec[0].endswith("/task.yaml")
    assert SENTINEL not in env["store"].read_text(rec[0])


def test_launch_skips_completed_job_in_s3(env):
    env["store"].write_text(LAYOUT.done(env["main"]), "")
    r = launch(env["main"], "--yes")
    assert r.exit_code == 0, r.output
    assert calls(env["log"]) == []
    assert not env["store"].exists(LAYOUT.files_complete(env["main"]))


def test_launch_restart_failed_rewrites_s3_marker(env):
    env["store"].write_text(LAYOUT.done(env["main"]), "failed\n")
    assert launch(env["main"], "--yes").exit_code == 0
    assert calls(env["log"]) == []  # failed without -r: skipped
    r = launch(env["main"], "--yes", "-r", "--dry")
    assert r.exit_code == 0, r.output
    assert env["store"].read_text(LAYOUT.done(env["main"])) == "failed\n"
    r = launch(env["main"], "--yes", "-r")
    assert r.exit_code == 0, r.output
    assert env["store"].read_text(LAYOUT.done(env["main"])) == "restart\n"
    assert len(calls(env["log"])) == 1


def test_launch_refuses_uncapped_hami_gpu(env):
    r = CliRunner().invoke(cli_mod.cli, ["launch", env["hami"], "--yes"])
    assert r.exit_code != 0
    assert "needs a nvidia.com/gpumem cap" in r.output
    assert env["store"].objects == {}
    assert calls(env["log"]) == []


def test_preflight_s3_unreachable_message(env):
    def boom():
        raise s3.StoreUnreachable("no route")
    env["store"].ping = boom
    r = CliRunner().invoke(cli_mod.cli, ["launch", env["main"], "--yes"])
    assert r.exit_code != 0
    assert "S3 unreachable — connect to the Stanford VPN" in r.output


def test_preflight_sky_api_unreachable(env, monkeypatch):
    monkeypatch.setenv("FAKE_SKY_API_RC", "1")
    r = CliRunner().invoke(cli_mod.cli, ["launch", env["main"], "--yes"])
    assert r.exit_code != 0
    assert "SkyPilot API unreachable — run sky-up" in r.output
    # --dry skips the sky check
    assert CliRunner().invoke(cli_mod.cli, ["launch", env["main"], "--dry"]).exit_code == 0


def test_launch_uploads_dep_closure_once(env):
    r = launch(env["main"], "--yes")
    assert r.exit_code == 0, r.output
    objs = env["store"].objects
    for dep in (env["mid"], env["base"]):
        assert LAYOUT.files(dep) + "r3.yaml" in objs
        assert objs[LAYOUT.files_complete(dep)] == b""
        assert objs[LAYOUT.complete(dep)] == b""
    assert objs[LAYOUT.output(env["base"]) + "b.txt"] == b"base-out"
    assert objs[LAYOUT.files_complete(env["main"])] == b""
    bundle = LAYOUT.git_bundle(env["git_path"], env["sha"])
    assert bundle in objs
    task = yaml.safe_load((env["log"] / "call0.yaml").read_text())
    assert set(task["envs"]["XR3SKY_DEPS"].split()) == {env["mid"], env["base"]}
    digest = bundle.split("/")[2]
    assert task["envs"]["XR3SKY_GIT"] == f"{digest}:{env['git_path']}:{env['sha']}"

    before = {k: v for k, v in objs.items() if "/runs/" not in k}
    uploads = []
    orig = env["store"].upload_file
    env["store"].upload_file = lambda p, k: (uploads.append(k), orig(p, k))
    r = launch(env["main"], "--yes")
    assert r.exit_code == 0, r.output
    assert uploads == []
    assert {k: v for k, v in objs.items() if "/runs/" not in k} == before


def test_launch_bundle_is_valid(env, tmp_path):
    assert launch(env["main"], "--yes").exit_code == 0
    data = env["store"].objects[LAYOUT.git_bundle(env["git_path"], env["sha"])]
    b = tmp_path / "x.bundle"
    b.write_bytes(data)
    # what the pod bootstrap does with a bundle
    _git("init", "-q", "--bare", str(tmp_path / "re"), cwd=tmp_path)
    _git("bundle", "unbundle", str(b), cwd=tmp_path / "re")
    assert _git("cat-file", "-t", env["sha"], cwd=tmp_path / "re") == "commit"
    # temporary ref (if any) was cleaned up in the laptop repo
    refs = _git("for-each-ref", "refs/xr3sky/", cwd=env["repo"] / env["git_path"])
    assert refs == ""


def test_launch_fresh_deletes_s3_output(env):
    out = LAYOUT.output(env["main"])
    env["store"].write_text(out + "partial.txt", "old")
    env["store"].write_text(LAYOUT.complete(env["main"]), "")
    r = launch(env["main"], "--yes", "--fresh")
    assert r.exit_code == 0, r.output
    assert not any(k.startswith(out) for k in env["store"].objects)
    assert not env["store"].exists(LAYOUT.complete(env["main"]))
    assert len(calls(env["log"])) == 1


def test_launch_upload_failure_writes_no_markers(env):
    def fail(p, k):
        raise RuntimeError("upload broke")
    env["store"].upload_file = fail
    r = CliRunner().invoke(cli_mod.cli, ["launch", env["main"], "--yes"])
    assert r.exit_code != 0
    assert not any(k.endswith(".complete") for k in env["store"].objects)
    assert calls(env["log"]) == []


def test_launch_confirm_threshold_prompts(env, tmp_path, monkeypatch):
    cfg = tmp_path / "xr3b.yaml"
    cfg.write_text(yaml.safe_dump({"sky": {"user": "tester", "secrets": [],
                                           "push_confirm_above": "1B"}}))
    monkeypatch.setenv("XR3_CONFIG", str(cfg))
    cli_mod._config.cache_clear()
    r = CliRunner().invoke(cli_mod.cli, ["launch", env["main"]], input="n\n")
    assert r.exit_code != 0
    assert calls(env["log"]) == []
    assert env["store"].objects == {}


def _queue(monkeypatch, name, status, job_id=7):
    monkeypatch.setenv("FAKE_SKY_QUEUE", json.dumps([{
        "job_id": job_id, "job_name": name, "status": status,
        "submitted_at": 1.0, "end_at": None}]))


def test_launch_skips_duplicate_of_running_managed_job(env, monkeypatch):
    name = f"tester-r3-{env['main'][:8]}"
    _queue(monkeypatch, name, "RUNNING")
    r = launch(env["main"], "--yes")
    assert r.exit_code == 0, r.output
    assert "already queued/running as managed job 7" in r.output
    assert calls(env["log"]) == []
    assert env["store"].objects == {}
    # --dry reports it but still prints the plan
    r = launch(env["main"], "--dry")
    assert "already queued/running as managed job 7" in r.output
    assert "--- task.yaml ---" in r.output
    # finished jobs with that name do not block; --force-duplicate overrides
    _queue(monkeypatch, name, "FAILED_SETUP")
    assert launch(env["main"], "--yes").exit_code == 0
    assert len(calls(env["log"])) == 1
    _queue(monkeypatch, name, "PENDING")
    assert launch(env["main"], "--yes", "--force-duplicate").exit_code == 0
    assert len(calls(env["log"])) == 2


def test_launch_fresh_relaunches_completed_job(env):
    env["store"].write_text(LAYOUT.done(env["main"]), "")
    local = env["repo"] / "jobs" / env["main"] / "output" / "done"
    local.write_text("")
    r = launch(env["main"], "--yes", "--fresh")
    assert r.exit_code == 0, r.output
    assert len(calls(env["log"])) == 1
    assert not env["store"].exists(LAYOUT.done(env["main"]))
    assert not local.exists()
    assert local.with_name("done.pre-fresh").exists()


def test_missing_default_secret_warns_and_is_omitted(env, tmp_path, monkeypatch):
    cfg = tmp_path / "xr3c.yaml"
    cfg.write_text(yaml.safe_dump({"sky": {"user": "tester",
                                           "secrets": ["WANDB_API_KEY", "PRESENT_TOK"]}}))
    monkeypatch.setenv("XR3_CONFIG", str(cfg))
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    monkeypatch.setenv("PRESENT_TOK", "x")
    cli_mod._config.cache_clear()
    r = launch(env["main"], "--yes")
    assert r.exit_code == 0, r.output
    assert "WARNING: secret WANDB_API_KEY" in r.stderr
    (c,) = calls(env["log"])
    assert "WANDB_API_KEY" not in c["argv"] and "PRESENT_TOK" in c["argv"]
    task = yaml.safe_load((env["log"] / "call0.yaml").read_text())
    assert "WANDB_API_KEY" not in task["secrets"] and "PRESENT_TOK" in task["secrets"]


def test_missing_job_secret_is_hard_error(env, tmp_path, monkeypatch):
    repo = r3.Repository(env["repo"])
    job = _commit(repo, tmp_path / "src_sec", {
        "run.sh": "echo s\n",
        "sky.yaml": yaml.safe_dump({"secrets": {"JOB_ONLY_TOKEN": None}})})
    monkeypatch.delenv("JOB_ONLY_TOKEN", raising=False)
    r = CliRunner().invoke(cli_mod.cli, ["launch", job, "--yes"])
    assert r.exit_code != 0
    assert "JOB_ONLY_TOKEN" in r.output
    assert env["store"].objects == {}
    assert calls(env["log"]) == []


def test_restart_rewrite_happens_only_after_uploads(env):
    env["store"].write_text(LAYOUT.done(env["main"]), "failed\n")
    def fail(p, k):
        raise RuntimeError("upload broke")
    env["store"].upload_file = fail
    r = CliRunner().invoke(cli_mod.cli, ["launch", env["main"], "--yes", "-r"])
    assert r.exit_code != 0
    assert env["store"].read_text(LAYOUT.done(env["main"])) == "failed\n"

