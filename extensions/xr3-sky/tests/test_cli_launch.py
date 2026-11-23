"""CLI tests for `xr3-sky launch` (needs r3 + click: run under the toolchain venv)."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

r3 = pytest.importorskip("r3")
pytest.importorskip("click")
yaml = pytest.importorskip("yaml")
from click.testing import CliRunner  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "xr3"))

import xr3skylib as lib  # noqa: E402
import xr3skys3 as s3  # noqa: E402


def _load_cli():
    path = HERE.parent / "xr3-sky"
    loader = importlib.machinery.SourceFileLoader("xr3sky_cli", str(path))
    spec = importlib.util.spec_from_loader("xr3sky_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cli_mod = _load_cli()
LAYOUT = lib.S3Layout("r3")
SENTINEL = "SENTINEL-SECRET-VALUE-9f8e7d"

FAKE_SKY = r'''#!/usr/bin/env python3
import json, os, shutil, sys
log = os.environ["FAKE_SKY_LOG"]
os.makedirs(log, exist_ok=True)
argv = sys.argv[1:]
if argv[:2] == ["api", "info"]:
    sys.exit(int(os.environ.get("FAKE_SKY_API_RC", "0")))
if argv[:2] == ["jobs", "queue"]:
    assert argv[2:4] == ["-o", "json"], argv
    print(os.environ.get("FAKE_SKY_QUEUE", "[]"))
    sys.exit(0)
n = len([f for f in os.listdir(log) if f.endswith(".json")])
with open(os.path.join(log, f"call{n}.json"), "w") as f:
    json.dump({"argv": argv, "env_names": sorted(os.environ),
               "aws_key_ok": os.environ.get("AWS_ACCESS_KEY_ID") == os.environ.get("EXPECT_KEY")}, f)
shutil.copy(argv[-1], os.path.join(log, f"call{n}.yaml"))
'''


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, src, files, deps=()):
    src.mkdir(parents=True)
    for name, text in files.items():
        (src / name).write_text(text)
    (src / "r3.yaml").write_text(yaml.safe_dump({"dependencies": list(deps)}))
    return repo.commit(r3.Job(src)).id


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo = r3.Repository.init(repo_path)

    # git dependency: a bare repo where r3 expects github.com/u/code
    work = tmp_path / "codework"
    work.mkdir()
    _git("init", "-q", cwd=work)
    (work / "lib.py").write_text("X = 1\n")
    _git("add", ".", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c", cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work)
    bare = repo_path / "git" / "github.com" / "u" / "code"
    bare.parent.mkdir(parents=True)
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    git_dep = {"repository": "https://github.com/u/code", "commit": sha,
               "destination": "code"}

    base = _commit(repo, tmp_path / "src_base", {"run.sh": "echo base\n"})
    (repo_path / "jobs" / base / "output" / "b.txt").write_text("base-out")
    (repo_path / "jobs" / base / "output" / "done").write_text("")
    mid = _commit(repo, tmp_path / "src_mid", {"run.sh": "echo mid\n"},
                  [{"job": base, "destination": "base"}, git_dep])
    (repo_path / "jobs" / mid / "output" / "m.txt").write_text("mid-out")
    (repo_path / "jobs" / mid / "output" / "done").write_text("")
    main = _commit(repo, tmp_path / "src_main", {"run.sh": "echo main\n"},
                   [{"job": mid, "destination": "mid"}])
    hami = _commit(repo, tmp_path / "src_hami", {
        "run.sh": "echo gpu\n",
        "sky.yaml": yaml.safe_dump({"resources": {"accelerators": "H100-80GB:1"}})})

    cfg = tmp_path / "xr3.yaml"
    cfg.write_text(yaml.safe_dump({"sky": {"user": "tester", "secrets": [],
                                           "push_confirm_above": "5GB"}}))
    sky = tmp_path / "fake-sky"
    sky.write_text(FAKE_SKY)
    sky.chmod(0o755)
    log = tmp_path / "skylog"

    store = s3.MemoryStore()
    store.ping = lambda: None
    monkeypatch.setattr(cli_mod, "_store", lambda cfg: store)
    cli_mod._config.cache_clear()
    monkeypatch.setenv("R3_REPOSITORY", str(repo_path))
    monkeypatch.setenv("XR3_CONFIG", str(cfg))
    monkeypatch.setenv("XR3SKY_SKY", str(sky))
    monkeypatch.setenv("FAKE_SKY_LOG", str(log))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", SENTINEL)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SENTINEL + "-2")
    monkeypatch.setenv("EXPECT_KEY", SENTINEL)
    return dict(repo=repo_path, store=store, log=log, base=base, mid=mid, main=main,
                hami=hami, sha=sha, git_path="git/github.com/u/code")


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

