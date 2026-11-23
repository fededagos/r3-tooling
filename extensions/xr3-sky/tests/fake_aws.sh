#!/bin/bash
# Fake `aws` for xr3-sky tests: maps s3://bucket/key to $FAKE_S3_ROOT/bucket/key.
# Supports `s3 cp` (file->s3, s3->file, `-` for stdin/stdout), `s3 sync` (both
# directions, recursive, no deletion) and `s3 ls`. Value-taking flags such as
# --endpoint-url/--region/--profile are skipped, other --flags ignored. The full argv
# is appended to $FAKE_AWS_LOG when set.
set -euo pipefail
: "${FAKE_S3_ROOT:?FAKE_S3_ROOT is not set}"
[ -n "${FAKE_AWS_LOG:-}" ] && echo "$*" >> "$FAKE_AWS_LOG"

args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --endpoint-url|--region|--profile|--exclude|--include) shift 2 ;;
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
        mkdir -p "$d"
        cp -RL "$s/." "$d/"
        ;;
    ls)
        p="$(local_path "${args[2]}")"
        if [ -e "$p" ] || [ -e "${p%/}" ]; then echo "$(basename "${p%/}")"; else exit 1; fi
        ;;
    *) echo "fake_aws: unsupported s3 command: $cmd" >&2; exit 2 ;;
esac
