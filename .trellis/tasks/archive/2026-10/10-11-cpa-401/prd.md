# CPA 401 credential rotation

## Goal

Provide a safe WebUI workflow for selecting accounts whose CPA Codex credential is returning 401, rerunning Codex OAuth through the existing CPA integration, verifying that a new usable credential exists, and only then cleaning the superseded CPA file(s). A failed or ambiguous retry must preserve the old credential and leave an auditable result.

## What I already know

* The user wants to batch-operate CPA accounts returning 401, rerun Codex, and remove the old 401 files after success.
* Existing WebUI routes already support single and bulk Codex retry: `webui/app.py:api_codex_retry` and `webui/app.py:api_codex_retry_bulk`.
* Existing CPA integration in `core/codex_oauth.py` calls `GET /v0/management/codex-auth-url`, `POST /v0/management/oauth-callback`, `GET /v0/management/auth-files`, and `GET /v0/management/auth-files/download?name=...`.
* Existing local credential operations in `webui/app.py` (`/api/codex/delete`, `/api/codex/delete-bulk`, `/api/codex/archive`, `/api/codex/archive-bulk`) act on the local SQLite records, not CPA auth-files.
* Existing local code search found no CPA auth-file delete endpoint. The CPA management API contract for deletion is therefore not established and must not be guessed.
* Historical Oracle read-only checks confirmed the deployed container can reach CPA and read CPA auth-files; those checks did not mutate production or expose secrets. The prior online configuration snapshot had `CODEX_AUTH_URL_SOURCE=local` and `ENABLE_CODEX_AUTO=False`, so manual retry and live configuration must be checked before rollout.

## Assumptions (temporary)

* The CPA service may overwrite/deduplicate by account or may create a new file; the application must identify old and new filenames from before/after snapshots rather than assume naming behavior.
* A successful local retry is not by itself proof that the new CPA file is usable; a post-retry CPA auth-file fetch and bounded validation are required.
* Cleanup must be disabled unless a documented CPA delete operation is discovered and validated against the deployed CPA version.

## Open Questions

* Confirm the CPA-supported delete/archive operation and its exact request contract, or explicitly ship a review-only cleanup queue when no delete API exists.
* Decide the user-facing cleanup mode after the contract is known: manual confirmation, opt-in automatic cleanup, or no remote cleanup.

## Requirements (evolving)

* Add a CPA rotation workflow that snapshots matching CPA auth-files before retry, records the selected account and candidate old files, runs the existing retry path, then refreshes CPA auth-files.
* Classify each item as `pending`, `retrying`, `verified`, `cleanup_ready`, `cleaned`, `failed`, or `ambiguous` with a reason that contains no credential values.
* Preserve old CPA files on OAuth failure, CPA timeout, missing post-retry file, multiple candidate matches, management 401, or unverified response.
* Never delete a CPA file merely because the local account status changed to `success`.
* If CPA deletion is supported, require exact filename identity from the before snapshot, re-check the post-retry file, require a successful verification response, and make cleanup idempotent with per-file results.
* Expose aggregate counts and bounded non-sensitive reasons in the API/UI; do not return or log credential payloads, access/refresh tokens, callback URLs, API keys, passwords, or TOTP values.
* Keep modern and legacy templates behaviorally equivalent if controls are added.
* Preserve existing manual retry, local delete, local archive, CPA download, and local export semantics.

## Acceptance Criteria

* [ ] A selected account can be retried through the existing CPA OAuth path without enabling automatic Codex registration.
* [ ] The workflow records a pre-retry CPA snapshot and can distinguish old filenames from post-retry candidates by normalized account identity.
* [ ] A retry failure leaves the old CPA file untouched and reports a bounded failure reason.
* [ ] A retry that produces no unique verified replacement is marked `ambiguous`/`failed` and cannot trigger cleanup.
* [ ] Cleanup, when supported and enabled, targets only exact pre-retry filenames and reports each remote result; no broad account/email deletion is used.
* [ ] Management API 401/403, timeout, 404, and unsupported-delete responses are handled without destructive fallback.
* [ ] Tests cover single/bulk selection, deduplication, retry failure, successful verification, ambiguous replacement, cleanup failure, auth protection, sensitive headers, and template parity.
* [ ] `py_compile`, extracted template `node --check`, focused tests, and `git diff --check` pass.

## Definition of Done

* Tests added or updated for API, service, and cross-layer state transitions.
* Backend and template quality checks pass.
* No secret values are added to logs, fixtures, responses, task files, or reports.
* Rollout is staged: dry-run/read-only snapshot first, one-account verification second, batch cleanup only after explicit enablement.
* Deployment and rollback steps are documented; no Oracle mutation occurs until the user explicitly requests implementation/deployment.

## Out of Scope

* Guessing or probing undocumented destructive CPA endpoints in production.
* Deleting local accounts or local SQLite credentials as a substitute for deleting CPA auth-files.
* Automatically changing CPA management keys, OAuth mode, proxy settings, SMS settings, or account identity settings.
* Reauthorizing all accounts without explicit selection and per-batch limits.

## Technical Approach

1. Add a small CPA credential-rotation service around the existing CPA list/download/request helpers instead of duplicating HTTP/header logic.
2. Use a before/after state machine: snapshot → retry → refresh → verify unique replacement → cleanup only if the exact CPA delete contract is configured and the user opted in.
3. Keep routes thin: validate request/limits, call the service, return bounded JSON; keep database writes and state transitions in existing storage/service patterns.
4. Use a dry-run/review queue as the default until the CPA delete contract is confirmed. If CPA has no supported delete API, offer exact old-file names for manual deletion in CPA rather than issuing guessed requests.

## Decision (ADR-lite)

**Context**: The project already retries Codex and reads CPA files, but local deletion endpoints do not delete CPA files and the CPA delete contract is not present in the source.

**Decision**: Implement verification-first rotation with cleanup gated behind an explicit, validated CPA delete adapter. Default to non-destructive review-only behavior; never infer deletion from retry success or filename conventions.

**Consequences**: The workflow may temporarily retain old 401 files when CPA deletion is unavailable, but it avoids credential loss and prevents a guessed endpoint from deleting the wrong account. A later CPA-specific adapter can enable cleanup without changing retry or verification semantics.

## Technical Notes

* `core/codex_oauth.py:532-600` lists/matches/downloads CPA auth-files; `core/codex_oauth.py:625-710` handles CPA authorization URL and callback.
* `webui/app.py:2620-2750` contains Codex WebUI controls; `webui/app.py:2620-2670` distinguishes local export/download/delete operations.
* `webui/app.py:2600-2750` and `webui/app.py:2628-2710` contain retry and batch-control patterns; verify exact current line numbers before implementation.
* `.trellis/spec/backend/error-handling.md`, `.trellis/spec/backend/logging-guidelines.md`, `.trellis/spec/backend/quality-guidelines.md`, and `.trellis/spec/guides/cross-layer-thinking-guide.md` are mandatory context.
* Historical online evidence is in `.trellis/tasks/archive/2026-10/10-10-oracle-codex-failures/research/` and prior read-only session artifacts under `/Users/jiangdk/.cache/pi/`; these are evidence only, not implementation instructions.
