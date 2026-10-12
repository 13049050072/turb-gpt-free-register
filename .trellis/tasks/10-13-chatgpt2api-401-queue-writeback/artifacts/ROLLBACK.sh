#!/bin/sh
set -eu
[ "$#" -eq 1 ] || { printf 'usage: %s TARGET_WORKTREE\n' "$0" >&2; exit 64; }
target=$1
case "$target" in /*) ;; *) target=$(cd "$target" && pwd);; esac
git -C "$target" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { printf 'not a git worktree: %s\n' "$target" >&2; exit 66; }
base_commit='178590c4479a67ea4910b202e353c4d7d90cf7e8'
paths='.trellis/spec/backend/chatgpt2api-credential-rotation.md core/chatgpt2api_401_service.py tests/test_chatgpt2api_401_service.py tests/test_chatgpt2api_401_templates.py tests/test_webui_chatgpt2api_401.py webui/app.py webui/templates/chatgpt2api_401.html'
for path in $paths; do
  mkdir -p "$target/$(dirname "$path")"
  git -C "$target" show "$base_commit:$path" > "$target/$path"
done
rm -rf "$target/.trellis/tasks/10-13-chatgpt2api-401-queue-writeback"
printf 'rollback complete: restored ChatGPT2API queue baseline %s\n' "$base_commit"
