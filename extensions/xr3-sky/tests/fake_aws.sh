#!/bin/bash
# Fake `aws` for xr3-sky tests: maps s3://bucket/key to $FAKE_S3_ROOT/bucket/key.
# Supports `s3 cp` (file->s3, s3->file, `-` for stdin/stdout), `s3 sync` (both
# directions, recursive, no deletion, --exclude globs) and `s3 ls`. Value-taking flags such as
# --endpoint-url/--region/--profile are skipped, other --flags ignored. The full argv
# is appended to $FAKE_AWS_LOG when set.
set -euo pipefail
: "${FAKE_S3_ROOT:?FAKE_S3_ROOT is not set}"
[ -n "${FAKE_AWS_LOG:-}" ] && echo "$*" >> "$FAKE_AWS_LOG"

args=()
excludes=()
while [ $# -gt 0 ]; do
    case "$1" in
        --exclude) excludes+=("$2"); shift 2 ;;
        --endpoint-url|--region|--profile|--include) shift 2 ;;
        --*) shift ;;
        *) args+=("$1"); shift ;;
    esac
done
[ "${args[0]:-}" = s3 ] || { echo "fake_aws: only 's3' is supported" >&2; exit 2; }

local_path() {  # s3://bucket/key -> $FAKE_S3_ROOT/bucket/key; others unchanged
    case "$1" in s3://*) echo "$FAKE_S3_ROOT/${1#s3://}" ;; *) echo "$1" ;; esac
}

cmd="${args[1]:-}"
case "$cmd" in
    cp)
        src="${args[2]}"; dst="${args[3]}"
        if [ "$src" = - ]; then
            d="$(local_path "$dst")"; mkdir -p "$(dirname "$d")"; cat > "$d"
        else
            s="$(local_path "$src")"
            [ -f "$s" ] || { echo "fatal error: $src not found" >&2; exit 1; }
            if [ "$dst" = - ]; then
                cat "$s"
            else
                d="$(local_path "$dst")"; mkdir -p "$(dirname "$d")"; cp "$s" "$d"
            fi
        fi
        ;;
    sync)
        s="$(local_path "${args[2]}")"; d="$(local_path "${args[3]}")"
        s="${s%/}"; d="${d%/}"
        [ -d "$s" ] || exit 0   # a missing prefix syncs nothing
        upload=0; case "${args[3]}" in s3://*) upload=1 ;; esac
        # FAKE_AWS_UPLOAD_SYNC_RC: 1 = fail an upload sync without copying anything;
        # any other value = copy, then exit with it (e.g. 2 = "some files skipped").
        if [ "$upload" = 1 ] && [ "${FAKE_AWS_UPLOAD_SYNC_RC:-0}" = 1 ]; then
            echo "fake_aws: injected upload failure" >&2; exit 1
        fi
        # --exclude patterns match the path relative to the source, `*` spanning `/`.
        (cd "$s" && find -L . -type f) | while IFS= read -r rel; do
            rel="${rel#./}"
            skip=0
            for pat in ${excludes[@]+"${excludes[@]}"}; do
                # shellcheck disable=SC2053
                [[ "$rel" == $pat ]] && skip=1
            done
            [ "$skip" = 1 ] && continue
            mkdir -p "$d/$(dirname "$rel")"
            cp -L "$s/$rel" "$d/$rel"
        done
        if [ "$upload" = 1 ]; then exit "${FAKE_AWS_UPLOAD_SYNC_RC:-0}"; fi
        ;;
    ls)
        p="$(local_path "${args[2]}")"
        if [ -e "$p" ] || [ -e "${p%/}" ]; then echo "$(basename "${p%/}")"; else exit 1; fi
        ;;
    *) echo "fake_aws: unsupported s3 command: $cmd" >&2; exit 2 ;;
esac
