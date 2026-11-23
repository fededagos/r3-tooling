"""Shared fixtures and helpers for the xr3-sky CLI tests (run under the toolchain venv)."""
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
if argv[:2] in (["jobs", "logs"], ["jobs", "cancel"]):
    with open(os.path.join(log, f"call{n}.json"), "w") as f:
        json.dump({"argv": argv}, f)
    print("FAKE-SKY " + " ".join(argv))
    sys.exit(int(os.environ.get("FAKE_SKY_RC", "0")))
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


