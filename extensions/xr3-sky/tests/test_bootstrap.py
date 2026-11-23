"""Tests for xr3sky_bootstrap.sh: fake S3 (fake_aws.sh) and a fake `r3` on PATH."""
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOOTSTRAP = HERE.parent / "xr3sky_bootstrap.sh"
FAKE_AWS = HERE / "fake_aws.sh"
HOUSE_RUN_SH = HERE.parents[2] / "examples" / "compute-job" / "run.sh"

JOB = "11111111-1111-4111-8111-111111111111"
DEP = "22222222-2222-4222-8222-222222222222"
PREFIX = "r3"

FAKE_R3 = r"""#!/bin/bash
set -euo pipefail
case "$1" in
  init)
    mkdir -p "$2/jobs" "$2/git"; echo "version: 1.0.0-beta.7" > "$2/r3.yaml" ;;
  rebuild-index)
    [ -f "$R3_REPOSITORY/r3.yaml" ]; touch "$R3_REPOSITORY/.index-rebuilt" ;;
  checkout)
    src="$R3_REPOSITORY/jobs/$2"; mkdir -p "$3"
    for f in "$src"/*; do
      [ "$(basename "$f")" = output ] || cp -R "$f" "$3/"
    done
    ln -s "$src/output" "$3/output" ;;
  *) echo "fake r3: unsupported $1" >&2; exit 2 ;;
esac
"""


def s3(tmp_path):
    return tmp_path / "s3" / "bucket" / PREFIX


def setup(tmp_path, run_inner="echo hi > output/x.txt\n", deps=(DEP,)):
    """Fake S3 with the committed job (and deps), a fake r3, and the env."""
    files = s3(tmp_path) / "jobs" / JOB / "files"
    files.mkdir(parents=True)
    shutil.copy(HOUSE_RUN_SH, files / "run.sh")
    (files / "run_inner.sh").write_text(run_inner)
    (files / "r3.yaml").write_text("dependencies: []\n")
    (s3(tmp_path) / "jobs" / JOB / "files.complete").write_text("")
    for dep in deps:
        out = s3(tmp_path) / "jobs" / dep / "output"
        out.mkdir(parents=True)
        (out / "data.txt").write_text("dep data")
        (out / "done").write_text("completed\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "r3").write_text(FAKE_R3)
    (bindir / "r3").chmod(0o755)
    FAKE_AWS.chmod(0o755)

    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "R3_JOB_ID": JOB,
        "XR3SKY_BUCKET": "bucket",
        "XR3SKY_PREFIX": PREFIX,
        "XR3SKY_DEPS": " ".join(deps),
        "XR3SKY_GIT": "",
        "XR3SKY_SYNC_INTERVAL": "1",
        "XR3SKY_ENDPOINT": "https://s3.public.example:443",
        "XR3SKY_ENDPOINT_INTERNAL": "http://rgw.unresolvable.invalid",
        "XR3SKY_AWS": str(FAKE_AWS),
        "XR3SKY_ROOT": str(tmp_path / "root"),
        "SCRATCH": str(tmp_path / "scratch"),
        "FAKE_S3_ROOT": str(tmp_path / "s3"),
        "FAKE_AWS_LOG": str(tmp_path / "aws.log"),
    }
    return env


def run(env, **kw):
    return subprocess.run(["bash", str(BOOTSTRAP)], env=env, capture_output=True,
                          text=True, timeout=60, **kw)


def remote_output(tmp_path):
    return s3(tmp_path) / "jobs" / JOB / "output"


def test_bootstrap_builds_sparse_repo(tmp_path):
    env = setup(tmp_path)
    r = run(env)
    assert r.returncode == 0, r.stderr
    root = tmp_path / "root"
    assert (root / "r3.yaml").read_text() == "version: 1.0.0-beta.7\n"
    assert (root / "jobs" / JOB / "run.sh").is_file()
    assert (root / "jobs" / JOB / "output").is_dir()
    assert (root / "jobs" / DEP / "output" / "data.txt").read_text() == "dep data"
    assert (root / ".index-rebuilt").exists()


def test_bootstrap_success_syncs_output_and_complete(tmp_path):
    env = setup(tmp_path)
    r = run(env)
    assert r.returncode == 0, r.stderr
    out = remote_output(tmp_path)
    assert (out / "x.txt").read_text() == "hi\n"
    assert (out / "done").read_text().strip() == "completed"
    assert (s3(tmp_path) / "jobs" / JOB / "output.complete").exists()


def test_bootstrap_failure_marks_failed(tmp_path):
    env = setup(tmp_path, run_inner="echo partial > output/p.txt\nexit 3\n")
    r = run(env)
    assert r.returncode == 3, r.stderr
    out = remote_output(tmp_path)
    assert (out / "done").read_text().strip() == "failed"
    assert (out / "p.txt").read_text() == "partial\n"
    assert not (s3(tmp_path) / "jobs" / JOB / "output.complete").exists()


def test_bootstrap_failure_after_restart_marks_failed(tmp_path):
    env = setup(tmp_path, run_inner="exit 5\n")
    out = remote_output(tmp_path)
    out.mkdir(parents=True)
    (out / "done").write_text("restart\n")
    r = run(env)
    assert r.returncode == 5, r.stderr
    assert (out / "done").read_text().strip() == "failed"


def test_bootstrap_resumes_partial_output(tmp_path):
    env = setup(tmp_path, run_inner=(
        "test -f output/ckpt.txt || exit 7\n"
        "cat output/ckpt.txt > output/seen.txt\n"))
    out = remote_output(tmp_path)
    out.mkdir(parents=True)
    (out / "ckpt.txt").write_text("step 42\n")
    (out / "done").write_text("restart\n")
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert (out / "seen.txt").read_text() == "step 42\n"
    assert (out / "ckpt.txt").read_text() == "step 42\n"
    assert (out / "done").read_text().strip() == "completed"


def test_bootstrap_endpoint_falls_back_to_public(tmp_path):
    env = setup(tmp_path)
    bindir = Path(env["PATH"].split(":")[0])
    getent = bindir / "getent"
    getent.write_text("#!/bin/bash\nexit 2\n")  # unresolvable on every platform
    getent.chmod(0o755)
    r = run(env)
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "aws.log").read_text().splitlines()
    assert calls
    assert all("--endpoint-url https://s3.public.example:443 " in c for c in calls)

    # When the internal host resolves, it is used instead.
    getent.write_text('#!/bin/bash\n[ "$2" = rgw.internal.svc ] && echo "10.0.0.1 $2"\n')
    (tmp_path / "aws.log").unlink()
    env["XR3SKY_ENDPOINT_INTERNAL"] = "http://rgw.internal.svc:8080"
    r = run(env)
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "aws.log").read_text().splitlines()
    assert all("--endpoint-url http://rgw.internal.svc:8080 " in c for c in calls)


def test_bootstrap_periodic_sync_and_sigterm_does_not_mark_failed(tmp_path):
    env = setup(tmp_path, run_inner="echo early > output/early.txt\nsleep 30\n")
    p = subprocess.Popen(["bash", str(BOOTSTRAP)], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    early = remote_output(tmp_path) / "early.txt"
    deadline = time.time() + 20
    while not early.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert early.exists(), "periodic sync did not upload output"
    p.send_signal(signal.SIGTERM)
    _, err = p.communicate(timeout=30)
    assert p.returncode == 143, err
    assert not (remote_output(tmp_path) / "done").exists()
    assert not (s3(tmp_path) / "jobs" / JOB / "output.complete").exists()


def test_bootstrap_missing_files_complete_marks_failed(tmp_path):
    env = setup(tmp_path)
    (s3(tmp_path) / "jobs" / JOB / "files.complete").unlink()
    r = run(env)
    assert r.returncode != 0
    assert "files.complete" in r.stderr
    # output/ was restored before the check, so the failure is recorded.
    assert (remote_output(tmp_path) / "done").read_text().strip() == "failed"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_bootstrap_git_bundles_two_commits_same_repo(tmp_path):
    import hashlib
    env = setup(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    commits = []
    for i in range(2):
        (src / "f.txt").write_text(str(i))
        _git("add", ".", cwd=src)
        _git("commit", "-q", "-m", str(i), cwd=src)
        commits.append(subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, text=True,
                                      capture_output=True, check=True).stdout.strip())
    repo_path = "git/github.com/u/r"
    digest = hashlib.sha256(repo_path.encode()).hexdigest()
    gdir = s3(tmp_path) / "git" / digest
    gdir.mkdir(parents=True)
    for i, c in enumerate(commits):
        _git("branch", "-f", f"b{i}", c, cwd=src)
        _git("bundle", "create", str(gdir / f"{c}.bundle"), f"b{i}", cwd=src)
    env["XR3SKY_GIT"] = " ".join(f"{digest}:{repo_path}:{c}" for c in commits)
    r = run(env)
    assert r.returncode == 0, r.stderr
    bare = tmp_path / "root" / repo_path
    for c in commits:
        subprocess.run(["git", "-C", str(bare), "cat-file", "-e", f"{c}^{{commit}}"],
                       check=True)
        # what r3 checkout does with the bare repo
        clone = tmp_path / f"co-{c[:7]}"
        clone.mkdir()
        _git("init", "-q", cwd=clone)
        _git("fetch", "-q", "--depth=1", str(bare), c, cwd=clone)


def test_bootstrap_failed_final_sync_never_uploads_completed(tmp_path):
    env = setup(tmp_path)
    env["FAKE_AWS_UPLOAD_SYNC_RC"] = "1"   # every upload sync fails, periodic and final
    env["XR3SKY_RETRY_DELAY"] = "0"
    r = run(env)
    assert r.returncode == 1, r.stderr
    assert "final output sync failed" in r.stderr
    assert not (remote_output(tmp_path) / "done").exists()
    assert not (s3(tmp_path) / "jobs" / JOB / "output.complete").exists()


def _s3_calls(tmp_path):
    """(index, verb, src, dst, argv) for each logged `s3 cp|sync` call."""
    out = []
    for i, line in enumerate((tmp_path / "aws.log").read_text().splitlines()):
        tok = line.split()
        j = tok.index("s3")
        if tok[j + 1] in ("cp", "sync"):
            out.append((i, tok[j + 1], tok[j + 2], tok[j + 3], line))
    return out


def test_bootstrap_done_uploaded_separately_after_sync(tmp_path):
    env = setup(tmp_path)
    r = run(env)
    assert r.returncode == 0, r.stderr
    calls = _s3_calls(tmp_path)
    up_syncs = [c for c in calls if c[1] == "sync" and c[3].startswith("s3://")]
    assert up_syncs and all("--exclude done" in c[4] for c in up_syncs)
    done_cp = [c[0] for c in calls if c[1] == "cp" and c[3].endswith(f"{JOB}/output/done")]
    complete_cp = [c[0] for c in calls if c[1] == "cp" and c[3].endswith("output.complete")]
    assert len(done_cp) == 1 and len(complete_cp) == 1
    assert max(c[0] for c in up_syncs) < done_cp[0] < complete_cp[0]


def test_bootstrap_prior_failed_is_not_success(tmp_path):
    env = setup(tmp_path, run_inner="echo ran > output/ran.txt\n")
    out = remote_output(tmp_path)
    out.mkdir(parents=True)
    (out / "done").write_text("failed\n")
    r = run(env)
    assert r.returncode == 1, r.stderr
    assert "not 'completed'" in r.stderr
    assert (out / "done").read_text().strip() == "failed"
    assert not (out / "ran.txt").exists()   # run.sh's guard skipped the job
    assert not (s3(tmp_path) / "jobs" / JOB / "output.complete").exists()


def test_bootstrap_completed_without_complete_marker_gets_certified(tmp_path):
    env = setup(tmp_path, run_inner="exit 9\n")   # must not run
    out = remote_output(tmp_path)
    out.mkdir(parents=True)
    (out / "result.txt").write_text("r")
    (out / "done").write_text("completed\n")
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert (out / "done").read_text().strip() == "completed"
    assert (s3(tmp_path) / "jobs" / JOB / "output.complete").exists()


def test_bootstrap_sync_skipped_files_is_a_warning(tmp_path):
    env = setup(tmp_path)
    env["FAKE_AWS_UPLOAD_SYNC_RC"] = "2"
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert "aws exit 2" in r.stderr
    assert (remote_output(tmp_path) / "x.txt").exists()
    assert (s3(tmp_path) / "jobs" / JOB / "output.complete").exists()


def test_bootstrap_rejects_malformed_git_triple(tmp_path):
    env = setup(tmp_path)
    env["XR3SKY_GIT"] = "abc:git/github.com/u/r:" + "0" * 40
    r = run(env)
    assert r.returncode != 0
    assert "bad sha256 digest" in r.stderr
