#!/bin/sh
set -eu
target=${1:-.}
base_commit='457c96f52c66a2ae2d356b50f5f391d161eac73a'
for path in \
  '.trellis/spec/backend/sub2api-credential-rotation.md' \
  'core/sub2api_admin.py' \
  'tests/test_sub2api_admin.py'
do
  mkdir -p "$target/$(dirname "$path")"
  git -C "$target" show "$base_commit:$path" > "$target/$path"
done
rm -rf "$target/.trellis/tasks/10-13-auto-credential-health-inspection/artifacts/sub2api-batch-refresh-timeout-fix"
printf 'rollback complete: restored Sub2API refresh baseline %s\n' "$base_commit"
