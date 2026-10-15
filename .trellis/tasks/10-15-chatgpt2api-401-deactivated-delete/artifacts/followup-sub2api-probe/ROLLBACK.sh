#!/bin/sh
# Restore the pre-change bytes of every path touched by the ChatGPT2API
# Sub2API error-only refresh follow-up (on top of follow-up 2). Self-contained: reads the sibling BASELINE.tar.gz,
# so the target may be a plain copy without git metadata.
set -eu
[ "$#" -eq 1 ] || { printf 'usage: %s TARGET_TREE\n' "$0" >&2; exit 64; }
target=$1
[ -d "$target" ] || { printf 'target is not a directory: %s\n' "$target" >&2; exit 66; }
here=$(cd "$(dirname "$0")" && pwd)
baseline="$here/BASELINE.tar.gz"
[ -f "$baseline" ] || { printf 'missing baseline archive: %s\n' "$baseline" >&2; exit 66; }
expected='1b1ed7aa80abd0635df76ae80765911663ea956a520c02c6ba7cd39e9f43a3ff'
if command -v shasum >/dev/null 2>&1; then
  actual=$(shasum -a 256 "$baseline" | cut -d' ' -f1)
else
  actual=$(sha256sum "$baseline" | cut -d' ' -f1)
fi
[ "$actual" = "$expected" ] || { printf 'baseline archive hash mismatch: %s\n' "$actual" >&2; exit 65; }
tar -xzf "$baseline" -C "$target"
printf 'rollback complete: restored %s paths from %s\n' "$(tar -tzf "$baseline" | wc -l | tr -d ' ')" "$baseline"
