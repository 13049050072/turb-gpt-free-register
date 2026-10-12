#!/bin/sh
set -eu
[ "$#" -eq 1 ] || { printf 'usage: %s TARGET_WORKTREE\n' "$0" >&2; exit 64; }
target=$1
case "$target" in /*) ;; *) target=$(cd "$target" && pwd);; esac
git -C "$target" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { printf 'not a git worktree: %s\n' "$target" >&2; exit 66; }
base_commit='0bd089693f01dfde022861b999feb2fff7f59a74'
paths='.trellis/spec/backend/sub2api-credential-rotation.md
core/sub2api_401_service.py
core/sub2api_admin.py
tests/test_sub2api_401_service.py
tests/test_sub2api_401_templates.py
tests/test_sub2api_admin.py
tests/test_webui_sub2api_401.py
webui/app.py
webui/templates/sub2api_401.html'
for path in $paths; do
  mkdir -p "$target/$(dirname "$path")"
  git -C "$target" show "$base_commit:$path" > "$target/$path"
done
rm -rf "$target/.trellis/tasks/10-13-sub2api-cleanup-duplicates"
printf 'rollback complete: restored Sub2API cleanup baseline %s\n' "$base_commit"
