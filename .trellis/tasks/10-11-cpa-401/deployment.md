# CPA 401 workflow rollout and rollback

## Rollout

1. Record the currently running Oracle image ID/tag and keep it available for rollback. Do not change CPA management keys, OAuth mode, proxy settings, or automatic registration settings.
2. Build a new image from the validated source and update only `TURB_IMAGE` in the Oracle Compose deployment at `/home/ubuntu/turb-gpt-free-register-compose`.
3. Run `docker compose --env-file compose.env -f compose.yaml up -d`, wait for the `turb-gpt-free-register` health check, and inspect bounded startup logs.
4. Confirm `/app/cpa_401_audit.json` is a symlink to `/data/cpa_401_audit.json`; the file contains only normalized account identity, exact CPA filenames, bounded reasons, timestamps, and workflow states.
5. Perform an authenticated read-only smoke check of `GET /api/cpa-401/state` and load `/cpa-401`. Do not include auth headers, cookies, management keys, or response payloads in deployment reports.
6. Run **检测 CPA 401** only. This calls the CPA v8 status refresh operation `POST /v8/management/credentials/refresh`, then the existing v0 auth-file list operation; it does not reauthorize or delete credentials.
7. For rollout verification, select one known sandbox account and run **补跑 Codex** with concurrency `1`. The existing OAuth retry worker runs, then CPA status/list/download validation executes automatically.
8. Require `verified` or `cleanup_ready` with one exact replacement filename. `failed` or `ambiguous` must retain all pre-retry filenames. Increase batches gradually up to 50 accounts and 8 workers only after the one-account verification.

## Operational constraints

- The default application has no CPA delete adapter. `/api/cpa-401/cleanup` is review-only and returns exact old filenames with `cleanup_supported=false`; it does not call a guessed remote endpoint and does not delete local SQLite records.
- CPA management `401/403`, timeout, `404`, unsupported refresh, missing replacement, duplicate/new ambiguous files, failed OAuth, or failed download validation leaves old credentials untouched.
- The audit state is persisted atomically in `/data/cpa_401_audit.json` with mode `0600` when supported. It never stores credential JSON, tokens, passwords, management keys, callback URLs, or OAuth responses.
- Existing manual retry, local delete/archive/export, CPA download, and Codex OAuth routes remain unchanged except that CPA callback values are no longer written to logs.

## Rollback

1. Set `TURB_IMAGE` back to the recorded prior image tag and run `docker compose --env-file compose.env -f compose.yaml up -d`.
2. Wait for the health check and verify the existing control page and prior Codex retry/download routes.
3. Leave `/data/cpa_401_audit.json` in place or archive it as non-sensitive operational evidence. The old image does not read it, and no SQLite/data rollback is required because the workflow adds no schema and performs no default remote cleanup.
4. If rollback follows an explicit future delete-adapter cleanup, restore affected CPA files only through the CPA product's documented recovery procedure; never substitute local SQLite records.
