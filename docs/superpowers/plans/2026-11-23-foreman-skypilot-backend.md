# Foreman SkyPilot Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Foreman shows SkyPilot managed jobs launched by `xr3-sky` (live status, history,
queue page) next to Slurm jobs.

**Architecture:**
- A new `foreman/skypilot.py` shells out to `sky jobs queue -o json`. It converts each
  managed job into the **existing** `QueueJob` and `SlurmRecord` dataclasses:
  - SkyPilot states are mapped to their Slurm equivalents.
  - `work_dir` is set to `r3/<full r3 id>`, so the existing `keys` matching works unchanged.
- A small `foreman/scheduler.py` defines the protocol both backends satisfy, plus
  `get_schedulers()`.
- `r3_extension.py` and `job_queue.py` iterate over the schedulers instead of calling
  `get_slurm()` directly.

**Tech Stack:** Flask, diskcache (memoization as in `slurm.py`), pytest. SkyPilot 0.13
CLI on `PATH`, reached through the Teleport tunnel (`sky-up`).

**Spec:** `docs/specs/2026-11-23-xr3-sky-design.md` §5.7, in the `r3-tooling` repo.

**Repo/branch:** `~/r3-toolchain/foreman`, a new branch `skypilot-backend` off
`feature/slurm-queue`, pushed to the `fork` remote (`fededagos/foreman`, private). Tests run
with `~/r3-toolchain/.venv/bin/python -m pytest tests -q`. Run
`uv pip install --python ~/r3-toolchain/.venv/bin/python pytest pillow` once first.

## Global Constraints

- **Managed-job names** are `<user>-r3-<first 8 chars of r3 id>`, which is how `xr3-sky` names them. Envs are *not* exposed by `sky jobs queue -o json`, so name matching is the only link.
- **`sky jobs queue -o json` row keys used:** `job_id`, `job_name`, `status`, `submitted_at`, `start_at`, `end_at`, `job_duration`, `resources` (e.g. `"1x[H100-80GB:1]"`, `"1x[CPU:1]"`), `infra`, `workspace`, `failure_reason`, `current_cluster_name`, `priority_class`. Timestamps are float epoch seconds or `null`.
- **State mapping** (SkyPilot state → the Slurm state stored in `.state`):

  | SkyPilot state | Stored as |
  |---|---|
  | `PENDING`, `STARTING` | `PENDING` |
  | `RUNNING`, `RECOVERING` | `RUNNING` |
  | `SUCCEEDED` | `COMPLETED` |
  | `FAILED`, `FAILED_*` | `FAILED` |
  | `CANCELLING`, `CANCELLED` | `CANCELLED` |
  | anything else | kept as-is |

  The original SkyPilot state goes in `QueueJob.reason`.
- `host` is `"skypilot"` on every converted object.
- **Dashboard link:** `{SKYPILOT_DASHBOARD_URL}/jobs/<job_id>`, with default `http://localhost:46581/dashboard`.
- **New config keys** (`config.py` + a `settings.py` page group `"skypilot"`):

  | Key | Default |
  |---|---|
  | `SKYPILOT_ENABLED` | `False` |
  | `SKYPILOT_USER` | `None`, which falls back to the local part of `$SKYPILOT_USER` |
  | `SKYPILOT_BINARY` | `"sky"` |
  | `SKYPILOT_CACHE_EXPIRATION` | `15` |
  | `SKYPILOT_DASHBOARD_URL` | as above |

- If `sky` fails (no tunnel), the error is logged and the backend returns empty results. It never raises into a page.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **The Teleport tunnel is down.** Every page still renders, and the SkyPilot jobs are simply absent. Tested in Task 2 (`test_backend_sky_failure_returns_empty`).
2. **Two r3 jobs share an 8-character id prefix, or no committed job matches.** Such a managed job is left unmatched instead of being attached to the wrong job. Tested in Task 1 (`test_resolve_prefix_ambiguous_or_missing`).
3. **Managed jobs not launched by `xr3-sky`** (names without `-r3-`, or another user's prefix). They don't match any r3 job, but they do appear on the queue page. Tested in Task 1 (`test_parse_foreign_names_kept_unmatched`).
4. **A job is relaunched (several rows with the same name).** Status uses the newest row, and the history keeps all of them sorted. Tested in Task 3 (`test_status_uses_newest_skypilot_record`).
5. **SkyPilot is disabled (the default).** No `sky` process is ever spawned. Tested in Task 2 (`test_backend_disabled_runs_nothing`).

---

### Task 1: Parse managed jobs (pure)

**Files:**
- Create: `foreman/skypilot.py`, `tests/test_skypilot.py`

**Interfaces — Produces:**
- `SKY_TO_SLURM_STATE: dict[str, str]`.
- `parse_resources(text: str) -> tuple[int, int]`, returning `(cpus, gpus)`. `"1x[H100-80GB:1]"` gives `(0, 1)` and `"1x[CPU:4]"` gives `(4, 0)`.
- `resolve_prefix(prefix: str, job_ids: Iterable[str]) -> Optional[str]`: the unique full id starting with `prefix`, otherwise `None`.
- `parse_managed_jobs(text: str, user: str, job_ids: Iterable[str]) -> tuple[list[QueueJob], list[SlurmRecord]]`.
  - Every row becomes a `SlurmRecord`.
  - Rows whose mapped state is `PENDING` or `RUNNING` also become a `QueueJob`.
  - `work_dir` is `f"r3/{full_id}"` when the prefix resolves, otherwise `""`.
  - `gpus` and `cpus` come from `parse_resources`.
  - `partition` is `infra or ""`; `nodelist` is `current_cluster_name or ""`; `account` is `workspace or ""`; `qos` is `priority_class or ""`.
  - `elapsed` is `timedelta(seconds=job_duration)` when set. `time_limit` is `None`. `nodes` is 1.

- [ ] **Step 1: Write the failing tests**

```python
ROW = {"job_id": 8802, "job_name": "federico-r3-0f3c9a7e", "status": "RECOVERING",
       "submitted_at": 1795095631.19, "start_at": None, "end_at": None, "job_duration": 12.4,
       "resources": "1x[H100-80GB:1]", "infra": "k8s/metamorphic.teleport.sh-do-k8s",
       "workspace": "federico", "failure_reason": None, "current_cluster_name": "c1",
       "priority_class": None}
IDS = ["0f3c9a7e-1234-4cde-9abc-0123456789ab", "aaaaaaaa-0000-4000-8000-000000000000"]

def test_parse_maps_state_and_keys():
    queue, records = skypilot.parse_managed_jobs(json.dumps([ROW]), "federico", IDS)
    assert queue[0].state == "RUNNING" and queue[0].reason == "RECOVERING"
    assert queue[0].keys == {"federico-r3-0f3c9a7e", IDS[0]}
    assert queue[0].gpus == 1 and queue[0].host == "skypilot"
    assert records[0].status == "running"

def test_parse_succeeded_is_completed_not_queued():
    queue, records = skypilot.parse_managed_jobs(json.dumps([{**ROW, "status": "SUCCEEDED"}]), "federico", IDS)
    assert queue == [] and records[0].status == "completed"

def test_resolve_prefix_ambiguous_or_missing():
    assert skypilot.resolve_prefix("0f3c9a7e", IDS) == IDS[0]
    assert skypilot.resolve_prefix("0f3c9a7e", IDS + ["0f3c9a7e-ffff-4fff-8fff-ffffffffffff"]) is None
    assert skypilot.resolve_prefix("deadbeef", IDS) is None

def test_parse_foreign_names_kept_unmatched():
    queue, _ = skypilot.parse_managed_jobs(json.dumps([{**ROW, "job_name": "federico-dev"}]), "federico", IDS)
    assert queue[0].work_dir == "" and queue[0].keys == {"federico-dev"}
```

- [ ] **Step 2: Run the tests to verify they fail.** Run `python -m pytest tests/test_skypilot.py -q`. Expected: FAIL (import error).
- [ ] **Step 3: Implement.** Import `QueueJob` and `SlurmRecord` from `foreman.slurm`. Don't duplicate them.
- [ ] **Step 4: Run the tests to verify they pass.** Expected: PASS.
- [ ] **Step 5: Commit.** Message: `feat: parse SkyPilot managed jobs into queue and history records`.

### Task 2: SkyPilot backend + scheduler protocol + settings

**Files:**
- Create: `foreman/scheduler.py`
- Modify:
  - `foreman/skypilot.py` (the backend class)
  - `foreman/config.py` (new keys)
  - `foreman/settings.py` (a `"skypilot"` group and its 5 `Setting`s)
  - `foreman/templates/settings/view.html.jinja`, only if groups are not rendered generically there (check first)
- Test: `tests/test_skypilot.py`, `tests/test_settings.py`

**Interfaces — Produces:**
- `class Scheduler(Protocol)` with these methods:
  - `get_jobs() -> dict[str, str]`: Slurm-style state by key
  - `get_history(since: Optional[date] = None) -> dict[str, list[SlurmRecord]]`
  - `get_records(job_id: str, since: Optional[date] = None) -> list[SlurmRecord]`
  - `get_queue(all_users: bool = False) -> list[QueueJob]`

  `Slurm` already satisfies this protocol, so no change is needed there.
- `class SkyPilot(Scheduler)`, built as `SkyPilot(binary: str, user: str, repository_root: Optional[Path], cache_expiration: int)`.
  - It runs `[binary, "jobs", "queue", "-o", "json"]`, adding `"-u"` when `all_users`, through `foreman.slurm.run_command(LOCAL_HOST, args)`. The call is memoized with `CACHE.memoize(expire=cache_expiration)`, exactly as `Slurm` does.
  - `job_ids` come from the names of `repository_root / "jobs"`.
  - `get_jobs` holds the newest record per key, for active records only.
- `get_skypilot() -> Optional[SkyPilot]`: per request, through `flask.g`. Returns `None` when `SKYPILOT_ENABLED` is false.
- `get_schedulers() -> list[Scheduler]`, in `scheduler.py`. Returns `[get_slurm()]`, plus `get_skypilot()` when it is enabled.

- [ ] **Step 1: Write the failing tests:**
  - `test_backend_parses_queue_via_run_command`: monkeypatch `foreman.skypilot.run_command` to return the JSON for `[ROW]`, then assert `get_queue()` has one job.
  - `test_backend_sky_failure_returns_empty`: `run_command` returns `None`, so `get_queue() == []` and `get_jobs() == {}`.
  - `test_backend_disabled_runs_nothing`: with `SKYPILOT_ENABLED=False` in the app config, `get_skypilot()` is `None` and `run_command` is never called. Use a spy.
  - `test_settings_page_lists_skypilot_group`: the settings page renders and contains `"SkyPilot"`. Follow the existing `test_settings.py` patterns.
- [ ] **Step 2: Run the tests to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite:** `python -m pytest tests -q`. Expected: PASS, with no regressions in `test_slurm.py` or `test_settings.py`.
- [ ] **Step 5: Commit.** Message: `feat: SkyPilot scheduler backend and settings`.

### Task 3: Use all schedulers for job status, history and the queue page

**Files:**
- Modify:
  - `foreman/r3_extension.py:73-100`: `_get_status` and `slurm_records`.
  - `foreman/job_queue.py`: `_context` and `_r3_jobs`.
  - `foreman/templates/job_queue/_jobs.html.jinja`: for `job.host == "skypilot"`, render the job id as a link to the dashboard URL (Global Constraints). In the `host` column, show `"SkyPilot"`.
- Test: `tests/test_skypilot.py`, `tests/test_job_queue.py`

**Interfaces:**
- **Consumes:** `get_schedulers()` from Task 2.
- **Behaviour:**
  - **`_get_status`.** The local `output/done` still wins, as today. Otherwise:
    1. The first scheduler whose `get_jobs()` has the id gives the state, mapped through `STATUS_BY_SLURM_STATE`.
    2. Failing that, take the newest record across all schedulers' `get_history()` (by `submit`) and use its `.status`.
  - **`slurm_records`.** Rename it `scheduler_records`, keeping `slurm_records` as an alias so the existing templates work. It concatenates the records of all schedulers, sorted by `submit`.
  - **Queue page.** `_context` concatenates `get_queue()` across schedulers. `"hosts"` includes `"skypilot"` when it is enabled. `_r3_jobs` needs no change, because the keys already contain the full id.

- [ ] **Step 1: Write the failing tests:**
  - `test_status_uses_newest_skypilot_record`: two `SUCCEEDED`/`FAILED` rows with the same name, where the newer one is `FAILED`. The job page status is `"failed"`.
  - `test_status_local_done_wins_over_skypilot`
  - `test_queue_page_shows_skypilot_job_with_dashboard_link`: the page HTML contains `http://localhost:46581/dashboard/jobs/8802` and the r3 job's name.
- [ ] **Step 2: Run the tests to verify they fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Run `python -m pytest tests -q`. Expected: PASS.
- [ ] **Step 5: Manual check, with `sky-up` running.**
  1. Run `SKYPILOT_ENABLED=True foreman --port 8080`. If foreman doesn't read env vars for config, set the value through the settings page instead.
  2. Open `/queue` while an `xr3-sky` job is running. The job appears, linked to its r3 job and to the dashboard.
  3. Stop the tunnel. The page still renders.
- [ ] **Step 6: Commit and push.** Message: `feat: show SkyPilot jobs in status, history and queue page`. Then run `git push -u fork skypilot-backend`.
