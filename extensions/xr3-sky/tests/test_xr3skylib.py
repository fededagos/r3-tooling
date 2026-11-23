import copy
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


# --- sky.yaml merge and validation -------------------------------------------

def gpu_task(acc, gpumem=None, **extra):
    t = {"resources": {"accelerators": f"{acc}:1"}, **extra}
    if gpumem:
        t["config"] = {"kubernetes": {"pod_config": {"spec": {"containers": [
            {"resources": {"limits": {"nvidia.com/gpumem": gpumem}}}]}}}}
    return t


def _prio(task, name):
    task = dict(task)
    task["config"] = {"kubernetes": {"pod_config": {"spec": {"priorityClassName": name}}}}
    return task


def _validate(task, whole_card=False, allow_high=False, infra_override=None):
    return lib.validate_and_fill(task, whole_card=whole_card, allow_high=allow_high,
                                 infra_override=infra_override)


def test_validate_hami_without_cap_refused():
    with pytest.raises(lib.SkySpecError, match="needs a nvidia.com/gpumem cap") as e:
        _validate(gpu_task("H100-80GB"))
    assert "8155" in str(e.value) and "81559" in str(e.value)


def test_validate_whole_card_fills_value():
    t = _validate(gpu_task("RTXPRO6000"), whole_card=True)
    assert lib.gpumem_of(t) == 97887
    assert lib.accelerator_of(t) == "RTXPRO6000"


def test_validate_whole_card_does_not_mutate_input():
    src = gpu_task("L40S")
    _validate(src, whole_card=True)
    assert "config" not in src


def test_validate_non_hami_needs_no_cap():
    _validate(gpu_task("RTX3090"))


def test_validate_cap_within_card_ok():
    t = _validate(gpu_task("H100-80GB", gpumem=8155))
    assert lib.gpumem_of(t) == 8155


def test_validate_gpumem_exceeds_whole_card():
    with pytest.raises(lib.SkySpecError, match="exceeds"):
        _validate(gpu_task("L40S", gpumem=46069))


def test_validate_resources_config_refused():
    t = {"resources": {"config": {}}}
    with pytest.raises(lib.SkySpecError, match="config: must be top-level"):
        _validate(t)


def test_validate_num_nodes_must_be_one():
    with pytest.raises(lib.SkySpecError, match="num_nodes must be 1"):
        _validate({"num_nodes": 2})
    _validate({"num_nodes": 1})


def test_validate_accelerator_list_refused():
    t = {"resources": {"accelerators": ["H100-80GB:1", "L40S:1"]}}
    with pytest.raises(lib.SkySpecError, match="GPU jobs must pin exactly one accelerator"):
        _validate(t)


def test_validate_accelerator_set_refused():
    t = {"resources": {"accelerators": {"H100-80GB", "L40S"}}}
    with pytest.raises(lib.SkySpecError, match="GPU jobs must pin exactly one accelerator"):
        _validate(t)


def test_validate_any_of_with_gpu_refused():
    t = {"resources": {"any_of": [{"accelerators": "L40S:1"}, {"cpus": 4}]}}
    with pytest.raises(lib.SkySpecError, match="GPU jobs must pin exactly one accelerator"):
        _validate(t)


def test_validate_ordered_with_gpu_refused():
    t = {"resources": {"ordered": [{"accelerators": {"RTX3090": 1}}]}}
    with pytest.raises(lib.SkySpecError, match="GPU jobs must pin exactly one accelerator"):
        _validate(t)


def test_validate_single_key_dict_accelerator_ok():
    t = {"resources": {"accelerators": {"RTX3090": 1}}}
    assert lib.accelerator_of(_validate(t)) == "RTX3090"


def test_validate_priority_class_unknown():
    with pytest.raises(lib.SkySpecError, match="priority class"):
        _validate(_prio({}, "urgent"))


def test_validate_high_priority_needs_flag():
    with pytest.raises(lib.SkySpecError, match="--allow-high"):
        _validate(_prio({}, "high"))
    _validate(_prio({}, "high"), allow_high=True)
    _validate(_prio({}, "low"))


def test_validate_do_volume_refused():
    with pytest.raises(lib.SkySpecError, match="DO has no persistent volume"):
        _validate({"volumes": {"/data": "do-scratch"}})
    _validate({"volumes": {"/data": "at-persist"}})


def test_validate_infra_override_contradiction():
    t = {"resources": {"infra": "k8s/at"}}
    with pytest.raises(lib.SkySpecError, match="--infra contradicts sky.yaml"):
        _validate(t, infra_override="k8s/morpho")
    assert _validate(t, infra_override="k8s/at")["resources"]["infra"] == "k8s/at"


def test_validate_infra_override_sets_when_absent():
    t = _validate({}, infra_override="k8s/morpho")
    assert t["resources"]["infra"] == "k8s/morpho"


def test_merge_job_wins_and_deep_merges():
    defaults = {"resources": {"cpus": 4, "infra": "k8s/at"}, "envs": {"A": "1", "B": "2"}}
    frag = {"resources": {"cpus": 8}, "envs": {"B": "3"}, "num_nodes": 1}
    out = lib.merge_task(defaults, frag)
    assert out == {"resources": {"cpus": 8, "infra": "k8s/at"},
                   "envs": {"A": "1", "B": "3"}, "num_nodes": 1}
    assert defaults["resources"]["cpus"] == 4 and frag["envs"] == {"B": "3"}


def test_merge_rejects_owned_keys():
    with pytest.raises(lib.SkySpecError):
        lib.merge_task({}, {"run": "echo hi"})


# --- done-state resolution and launch decision -------------------------------

def test_effective_done_s3_wins():
    assert lib.effective_done("failed\n", "completed\n") == "failed"
    assert lib.effective_done(None, "completed\n") == "completed"
    assert lib.effective_done(None, None) is None


def test_effective_done_classifies_bodies():
    assert lib.effective_done("restart\n", None) == "restart"
    assert lib.effective_done("  failed  \n", None) == "failed"
    assert lib.effective_done("anything else\n", None) == "completed"


def test_effective_done_empty_marker_is_completed():
    assert lib.effective_done("", None) == "completed"
    assert lib.effective_done(None, "") == "completed"


def test_launch_decision_table():
    assert lib.launch_decision(None, False) == "launch"
    assert lib.launch_decision("completed", True) == "skip"
    assert lib.launch_decision("restart", False) == "launch"
    assert lib.launch_decision("failed", False) == "skip"
    assert lib.launch_decision("failed", True) == "launch_rewrite_restart"


def test_files_complete_key():
    assert lib.S3Layout("r3").files_complete("J") == "r3/jobs/J/files.complete"


# --- render_task -------------------------------------------------------------

RCFG = {"bucket": "bk", "prefix": "r3", "sync_interval": 300,
        "endpoint": "https://s3.ext", "endpoint_internal": "http://s3.int"}


def _render(**kw):
    args = dict(name="n", job_id="ID", task={"resources": {"cpus": 2}}, deps=["D1"], git=[],
                sky_cfg=RCFG, r3_ref="a65b646", bootstrap_text="echo boot",
                secret_names=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"])
    args.update(kw)
    return lib.render_task(**args)


def test_render_task_owns_name_setup_run_and_never_embeds_secret_values():
    t = lib.render_task(name="federico-r3-0f3c9a7e", job_id="ID", task={"resources": {"cpus": 2}},
                        deps=["D1"], git=[], sky_cfg=RCFG, r3_ref="a65b646",
                        bootstrap_text="echo boot", secret_names=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"])
    assert t["name"] == "federico-r3-0f3c9a7e"
    assert t["envs"]["R3_JOB_ID"] == "ID" and t["envs"]["XR3SKY_DEPS"] == "D1"
    assert t["secrets"] == {"AWS_ACCESS_KEY_ID": None, "AWS_SECRET_ACCESS_KEY": None}
    assert "a65b646" in t["setup"] and t["run"] == "echo boot"
    assert t["resources"] == {"cpus": 2}


def test_render_task_envs_exact_and_strings():
    t = _render(deps=["D1", "D2"], git=[("abc", "git/foo", "c0ffee"), ("def", "git/bar", "beef")])
    assert t["envs"] == {
        "R3_JOB_ID": "ID", "XR3SKY_BUCKET": "bk", "XR3SKY_PREFIX": "r3",
        "XR3SKY_DEPS": "D1 D2", "XR3SKY_GIT": "abc:git/foo:c0ffee def:git/bar:beef",
        "XR3SKY_SYNC_INTERVAL": "300", "XR3SKY_ENDPOINT": "https://s3.ext",
        "XR3SKY_ENDPOINT_INTERNAL": "http://s3.int"}
    assert all(isinstance(v, str) for v in t["envs"].values())


def test_render_task_fragment_envs_kept_and_overridden():
    t = _render(task={"envs": {"FOO": "1", "XR3SKY_BUCKET": "evil", "R3_JOB_ID": "x"}})
    assert t["envs"]["FOO"] == "1"
    assert t["envs"]["XR3SKY_BUCKET"] == "bk" and t["envs"]["R3_JOB_ID"] == "ID"


def test_render_task_secrets_merge_dedupe_order():
    t = _render(task={"secrets": {"HF_TOKEN": None, "AWS_ACCESS_KEY_ID": None}},
                secret_names=["WANDB", "HF_TOKEN"])
    assert list(t["secrets"]) == ["WANDB", "HF_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    assert all(v is None for v in t["secrets"].values())


def test_render_task_setup_idempotent_snippet():
    s = _render()["setup"]
    assert "command -v uv ||" in s and "astral.sh/uv/install.sh" in s
    assert 'uv tool install "r3 @ git+https://github.com/mtangemann/r3@a65b646"' in s
    assert "uv tool install awscli" in s
    assert "UV_CACHE_DIR=$HOME/.cache/uv" in s and ".local/bin" in s


def test_render_task_does_not_mutate_inputs_and_keeps_keys():
    task = {"resources": {"cpus": 2}, "config": {"a": 1}, "volumes": {"/m": "v"}, "num_nodes": 2,
            "envs": {"FOO": "1"}, "secrets": {"S": None}}
    snap = copy.deepcopy(task)
    names, deps, git = ["X"], ["D"], [("a", "git/b", "c")]
    sn = (list(names), list(deps), list(git))
    t = _render(task=task, secret_names=names, deps=deps, git=git)
    assert task == snap and (names, deps, git) == sn
    for k in ("resources", "config", "volumes", "num_nodes"):
        assert t[k] == task[k]
    t["envs"]["Z"] = "1"; t["resources"]["cpus"] = 9
    assert "Z" not in task["envs"] and task["resources"]["cpus"] == 2
