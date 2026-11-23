import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "xr3"))

import xr3config
import xr3skylib as lib


def test_parse_size():
    assert lib.parse_size("100MB") == 100_000_000
    assert lib.parse_size("5GB") == 5_000_000_000
    assert lib.parse_size("1234") == 1234


def test_sky_user_prefers_config_then_env():
    assert lib.sky_user({"user": "alice"}, {"SKYPILOT_USER": "bob@x.com"}) == "alice"
    assert lib.sky_user({"user": None}, {"SKYPILOT_USER": "federico@metamorphic.com"}) == "federico"
    with pytest.raises(ValueError):
        lib.sky_user({"user": None}, {})


def test_managed_job_name_roundtrip():
    jid = "0f3c9a7e-1234-4cde-9abc-0123456789ab"
    name = lib.managed_job_name("federico", jid)
    assert name == "federico-r3-0f3c9a7e"
    assert lib.job_id_prefix_from_name(name, "federico") == "0f3c9a7e"
    assert lib.job_id_prefix_from_name("federico-dev", "federico") is None


def test_s3_layout():
    L = lib.S3Layout("r3")
    assert L.files("ID") == "r3/jobs/ID/files/"
    assert L.output("ID") == "r3/jobs/ID/output/"
    assert L.done("ID") == "r3/jobs/ID/output/done"
    assert L.complete("ID") == "r3/jobs/ID/output.complete"
    assert L.run_record("ID", "T") == "r3/runs/ID/T/task.yaml"
    assert L.git_bundle("github.com/a/b", "abc").startswith("r3/git/")
    assert L.git_bundle("github.com/a/b", "abc").endswith("/abc.bundle")


def test_config_defaults_have_sky():
    cfg = xr3config.load_config("/nonexistent")
    assert cfg["sky"]["bucket"] == "federico-r3"
    assert cfg["sky"]["defaults"]["resources"] == {"cpus": 4, "memory": 16}
