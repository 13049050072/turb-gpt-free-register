#!/bin/sh
set -eu
[ "$#" -eq 1 ] || { printf 'usage: %s TARGET_WORKTREE\n' "$0" >&2; exit 64; }
target=$1
case "$target" in /*) ;; *) target=$(cd "$target" && pwd);; esac
git -C "$target" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { printf 'not a git worktree: %s\n' "$target" >&2; exit 66; }
base_commit='5cf517534665431f9b62fb274883be4032eedffa'
paths='.trellis/spec/backend/sub2api-credential-rotation.md tests/test_sub2api_401_templates.py webui/templates/sub2api_401.html'
for path in $paths; do
  mkdir -p "$target/$(dirname "$path")"
  git -C "$target" show "$base_commit:$path" > "$target/$path"
done
rm -rf "$target/.trellis/tasks/10-13-10-13-sub2api-401-status-polling"
printf 'rollback complete: restored Sub2API-401 polling baseline %s\n' "$base_commit"
