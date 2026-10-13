#!/bin/sh
set -eu
[ "$#" -eq 1 ] || { printf 'usage: %s TARGET_WORKTREE\n' "$0" >&2; exit 64; }
target=$1
case "$target" in /*) ;; *) target=$(cd "$target" && pwd);; esac
git -C "$target" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { printf 'not a git worktree: %s\n' "$target" >&2; exit 66; }
base_commit='a69150f12a8878588884993979537941b46c8d9d'
tracked='.gitignore Dockerfile core/chatgpt2api_401_service.py tests/test_chatgpt2api_401_service.py tests/test_chatgpt2api_401_templates.py tests/test_sub2api_401_templates.py webui/app.py webui/auth.py webui/templates/chatgpt2api_401.html webui/templates/sub2api_401.html'
for path in $tracked; do
  mkdir -p "$target/$(dirname "$path")"
  git -C "$target" show "$base_commit:$path" > "$target/$path"
done
rm -f "$target/.trellis/spec/backend/credential-health-scheduler.md"
rm -f "$target/core/credential_health_scheduler.py"
rm -f "$target/tests/test_credential_health_scheduler.py"
rm -f "$target/tests/test_webui_credential_health_scheduler.py"
rm -rf "$target/.trellis/tasks/10-13-auto-credential-health-inspection"
printf 'rollback complete: restored automatic inspection baseline %s\n' "$base_commit"
