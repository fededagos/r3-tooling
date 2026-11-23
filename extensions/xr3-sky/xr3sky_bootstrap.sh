#!/bin/bash
# xr3-sky pod bootstrap: the SkyPilot task's `run:` (design spec §5.5).
#
# The pod is disposable; Ceph S3 is the only state that persists. This script:
#   1. pre-populates jobs/<id>/output/ from S3 (resume after preemption / restart),
#   2. rebuilds a sparse r3 repo at $XR3SKY_ROOT (r3.yaml, the job, its job
#      dependencies, git bundles) and runs `r3 rebuild-index`,
#   3. syncs output/ to S3 every $XR3SKY_SYNC_INTERVAL seconds in the background,
#   4. runs the job's unchanged house `run.sh`,
#   5. on any exit: stops the sync loop, marks `failed` if run.sh failed, does a
#      final sync (everything but `done`), then uploads `done` on its own, then
#      writes output.complete when output/done is `completed`.
# Exit status: run.sh's (143 on a termination signal); 1 if the final sync or a
# marker upload fails, or if run.sh exits 0 without output/done = completed.
#
# Outputs are never deleted remotely (no `sync --delete`). Nothing is uploaded to
# S3 until output/ has been fully pre-populated from it, so a bootstrap failure
# before that point cannot clobber remote state. Bash 3.2 compatible. Never `set -x`:
# the environment carries credentials.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"  # uv tool installs (r3, aws) live here

log() { echo "[xr3sky] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# --- configuration ------------------------------------------------------------
: "${R3_JOB_ID:?R3_JOB_ID is not set}"
: "${XR3SKY_BUCKET:?XR3SKY_BUCKET is not set}"
: "${XR3SKY_PREFIX:?XR3SKY_PREFIX is not set}"
: "${XR3SKY_ENDPOINT:?XR3SKY_ENDPOINT is not set}"
XR3SKY_ENDPOINT_INTERNAL="${XR3SKY_ENDPOINT_INTERNAL:-}"
XR3SKY_DEPS="${XR3SKY_DEPS:-}"
XR3SKY_GIT="${XR3SKY_GIT:-}"
XR3SKY_SYNC_INTERVAL="${XR3SKY_SYNC_INTERVAL:-300}"
XR3SKY_AWS="${XR3SKY_AWS:-aws}"
XR3SKY_ROOT="${XR3SKY_ROOT:-$HOME/r3_repo}"
XR3SKY_RETRY_DELAY="${XR3SKY_RETRY_DELAY:-5}"   # final-sync backoff unit (seconds)
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-stanford}"

valid_id() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }
valid_id "$R3_JOB_ID" || die "invalid R3_JOB_ID: $R3_JOB_ID"
set -f  # unquoted id lists below must never glob
for _dep in $XR3SKY_DEPS; do valid_id "$_dep" || die "invalid dependency id: $_dep"; done
set +f
[[ "$XR3SKY_SYNC_INTERVAL" =~ ^[1-9][0-9]*$ ]] || die "XR3SKY_SYNC_INTERVAL must be a positive integer"
[[ "$XR3SKY_RETRY_DELAY" =~ ^[0-9]+$ ]] || die "XR3SKY_RETRY_DELAY must be a non-negative integer"

S3_BASE="s3://$XR3SKY_BUCKET/${XR3SKY_PREFIX%/}"
S3_JOB="$S3_BASE/jobs/$R3_JOB_ID"
JOB_DIR="$XR3SKY_ROOT/jobs/$R3_JOB_ID"
OUT="$JOB_DIR/output"

TMPD="$(mktemp -d)"
STOP_FILE="$TMPD/stop-sync"
OUTPUT_READY=0   # 1 once output/ mirrors S3; only then may finish() upload
INTERRUPTED=0    # 1 on SIGTERM/SIGINT/SIGHUP (preemption, cancel): never mark failed
SYNC_PID=""
RUN_PID=""

# --- endpoint -------------------------------------------------------------------
endpoint_host() {  # http(s)://host[:port][/path] -> host
    local h="${1#*://}"
    echo "${h%%[:/]*}"
}
ENDPOINT="$XR3SKY_ENDPOINT"
if [ -n "$XR3SKY_ENDPOINT_INTERNAL" ] && command -v getent >/dev/null 2>&1 \
        && getent hosts "$(endpoint_host "$XR3SKY_ENDPOINT_INTERNAL")" >/dev/null 2>&1; then
    ENDPOINT="$XR3SKY_ENDPOINT_INTERNAL"
fi
log "S3 endpoint: $ENDPOINT"

s3() { "$XR3SKY_AWS" --endpoint-url "$ENDPOINT" s3 "$@"; }

# Upload output/ except the top-level `done` marker. `aws s3 sync` uploads in
# parallel and in no fixed order, so `done` is uploaded on its own (upload_done)
# only after everything else is in S3: S3 never holds done=completed with files
# missing. Exit code 2 means some files were skipped (e.g. a dangling symlink); that
# is logged and tolerated so it cannot block output.complete.
sync_output() {
    local rc=0
    s3 sync "$OUT/" "$S3_JOB/output/" --exclude done --only-show-errors || rc=$?
    if [ "$rc" -eq 2 ]; then
        log "WARNING: output sync skipped some files (aws exit 2)"
        rc=0
    fi
    return "$rc"
}

upload_done() {  # no-op when there is no local marker
    [ -f "$OUT/done" ] || return 0
    s3 cp "$OUT/done" "$S3_JOB/output/done" --only-show-errors
}

sync_loop() {
    set +m
    local waited=0
    while [ ! -e "$STOP_FILE" ]; do
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge "$XR3SKY_SYNC_INTERVAL" ] && [ ! -e "$STOP_FILE" ]; then
            sync_output || log "WARNING: periodic output sync failed (will retry)"
            waited=0
        fi
    done
}

done_state() { [ -f "$OUT/done" ] && tr -d '[:space:]' < "$OUT/done" || true; }

# --- exit handling ----------------------------------------------------------------
on_signal() {
    INTERRUPTED=1
    log "received termination signal; stopping run.sh and syncing output"
    if [ -n "$RUN_PID" ] && kill -0 "$RUN_PID" 2>/dev/null; then
        # run.sh leads its own process group: signal run_inner.sh and its children too.
        kill -TERM -- "-$RUN_PID" 2>/dev/null || kill -TERM "$RUN_PID" 2>/dev/null || true
        local i=0  # give run.sh a moment to flush checkpoints
        while [ "$i" -lt 10 ] && kill -0 "$RUN_PID" 2>/dev/null; do sleep 1; i=$((i + 1)); done
        kill -KILL -- "-$RUN_PID" 2>/dev/null || true
    fi
    exit 143
}

# Time budget after a termination signal (it must fit the pod's grace period):
# <=10s for run.sh to exit (then SIGKILL), <=5s to stop the sync loop (then its
# process group is killed), then the final sync (proportional to unsynced data; up to
# 3 attempts with XR3SKY_RETRY_DELAY*1 and *2 backoff), then two small uploads.
finish() {
    local rc=$? state attempt i
    set +e
    trap '' TERM INT HUP   # nothing may interrupt saving the outputs

    # 1. Stop the sync loop: let an in-flight sync finish for up to 5s, then kill the
    #    loop's whole process group so no orphaned upload lands after the final sync.
    if [ -n "$SYNC_PID" ]; then
        touch "$STOP_FILE"
        i=0
        while [ "$i" -lt 5 ] && kill -0 "$SYNC_PID" 2>/dev/null; do sleep 1; i=$((i + 1)); done
        kill -TERM -- "-$SYNC_PID" 2>/dev/null
        wait "$SYNC_PID" 2>/dev/null
    fi

    if [ "$OUTPUT_READY" != 1 ]; then
        log "bootstrap failed before output/ was restored from S3 (exit $rc); S3 left untouched"
        rm -rf "$TMPD"
        exit "$rc"
    fi

    # 2. Mark failure (not on preemption/cancel: those resume from S3).
    state="$(done_state)"
    if [ "$rc" -ne 0 ] && [ "$INTERRUPTED" = 0 ] && { [ -z "$state" ] || [ "$state" = restart ]; }; then
        log "job failed with exit $rc; writing output/done = failed"
        echo failed > "$OUT/done"
    fi

    # 3. Final sync (without `done`), retried: the last chance to save the outputs.
    local synced=0
    for attempt in 1 2 3; do
        if sync_output; then synced=1; break; fi
        log "WARNING: final output sync attempt $attempt failed"
        [ "$attempt" -lt 3 ] && sleep $((attempt * XR3SKY_RETRY_DELAY))
    done
    if [ "$synced" != 1 ]; then
        log "ERROR: final output sync failed; outputs may be missing from S3; output/done not uploaded"
        [ "$rc" -eq 0 ] && rc=1
        rm -rf "$TMPD"
        exit "$rc"
    fi

    # 4. Only now the `done` marker, then output.complete for a completed job.
    state="$(done_state)"
    if ! upload_done; then
        log "ERROR: could not upload output/done"
        [ "$rc" -eq 0 ] && rc=1
    elif [ "$state" = completed ]; then
        : > "$TMPD/empty"
        if s3 cp "$TMPD/empty" "$S3_JOB/output.complete" --only-show-errors; then
            log "output synced; wrote output.complete"
        else
            log "ERROR: could not write output.complete"
            [ "$rc" -eq 0 ] && rc=1
        fi
    fi

    # 5. run.sh's guard exits 0 on any non-`restart` marker (e.g. a prior `failed`):
    #    that must not look like success.
    if [ "$rc" -eq 0 ] && [ "$INTERRUPTED" = 0 ] && [ "$state" != completed ]; then
        log "ERROR: run.sh exited 0 but output/done is '${state:-absent}', not 'completed'"
        rc=1
    fi

    rm -rf "$TMPD"
    exit "$rc"
}
trap finish EXIT
trap on_signal TERM INT HUP

# --- 1. restore output/ from S3 (resume) ------------------------------------------
mkdir -p "$XR3SKY_ROOT/jobs" "$XR3SKY_ROOT/git" "$OUT"
chmod u+w "$XR3SKY_ROOT/jobs" "$JOB_DIR"
log "restoring output from $S3_JOB/output/"
s3 sync "$S3_JOB/output/" "$OUT/" --only-show-errors
chmod -R u+w "$OUT"
OUTPUT_READY=1
log "output/done on entry: '$(done_state)'"

# --- 2. sparse r3 repo ---------------------------------------------------------------
s3 ls "$S3_JOB/files.complete" >/dev/null 2>&1 \
    || die "job files incomplete in S3 (no $S3_JOB/files.complete)"

# r3.yaml exactly as this pod's r3 writes it (r3 is pinned to the laptop's commit).
r3 init "$TMPD/init" >/dev/null
cp -f "$TMPD/init/r3.yaml" "$XR3SKY_ROOT/r3.yaml"

log "downloading job $R3_JOB_ID"
s3 sync "$S3_JOB/files/" "$JOB_DIR/" --only-show-errors
[ -f "$JOB_DIR/run.sh" ] || die "job $R3_JOB_ID has no run.sh"

set -f
for dep in $XR3SKY_DEPS; do
    log "downloading dependency $dep"
    mkdir -p "$XR3SKY_ROOT/jobs/$dep/output"
    # files/ is absent for output-only dependencies; a missing prefix syncs nothing.
    s3 sync "$S3_BASE/jobs/$dep/files/" "$XR3SKY_ROOT/jobs/$dep/" --only-show-errors
    s3 sync "$S3_BASE/jobs/$dep/output/" "$XR3SKY_ROOT/jobs/$dep/output/" --only-show-errors
done

for triple in $XR3SKY_GIT; do
    digest="${triple%%:*}"
    commit="${triple##*:}"
    repo_path="${triple#*:}"
    repo_path="${repo_path%:*}"
    { [ -n "$digest" ] && [ -n "$repo_path" ] && [ -n "$commit" ] && [ "$digest:$repo_path:$commit" = "$triple" ]; } \
        || die "malformed XR3SKY_GIT entry: $triple"
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die "bad sha256 digest in XR3SKY_GIT entry: $triple"
    [[ "$commit" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || die "bad commit in XR3SKY_GIT entry: $triple"
    case "$repo_path" in /*|*..*) die "unsafe git repository path: $repo_path" ;; esac
    # r3's GitDependency.repository_path already starts with git/ (git/github.com/u/r).
    case "$repo_path" in git/*) dest="$XR3SKY_ROOT/$repo_path" ;; *) dest="$XR3SKY_ROOT/git/$repo_path" ;; esac
    bundle="$TMPD/$commit.bundle"
    log "fetching git bundle for $repo_path@$commit"
    s3 cp "$S3_BASE/git/$digest/$commit.bundle" "$bundle" --only-show-errors
    if [ -d "$dest" ]; then
        git -C "$dest" bundle unbundle "$bundle" >/dev/null
    else
        mkdir -p "$(dirname "$dest")"
        git clone --quiet --bare "$bundle" "$dest"
    fi
    # Pin the commit with a ref so `git fetch --depth=1 <dest> <commit>` can want it.
    git -C "$dest" update-ref "refs/xr3sky/$commit" "$commit" \
        || die "commit $commit not found in bundle for $repo_path"
    rm -f "$bundle"
done
set +f

# Output-only dependencies have no r3.yaml/metadata.yaml, which rebuild-index needs;
# checkout of a committed job does not use the index, so do not abort on this.
R3_REPOSITORY="$XR3SKY_ROOT" r3 rebuild-index \
    || log "WARNING: r3 rebuild-index failed; continuing (checkout does not need the index)"

# --- 3. background sync ------------------------------------------------------------------
set -m  # own process group, so finish() can kill the loop and any in-flight aws
sync_loop &
SYNC_PID=$!
set +m

# --- 4. run the job --------------------------------------------------------------------
cd "$JOB_DIR"
log "running run.sh for $R3_JOB_ID"
set -m  # own process group, so a termination signal can reach the whole job tree
R3_REPOSITORY="$XR3SKY_ROOT" bash run.sh &
RUN_PID=$!
set +m
run_rc=0
wait "$RUN_PID" || run_rc=$?
RUN_PID=""
log "run.sh exited with $run_rc"
exit "$run_rc"
