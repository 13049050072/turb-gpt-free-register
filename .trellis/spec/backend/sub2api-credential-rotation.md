# Sub2API Credential Rotation

> Executable contract for detecting unauthorized OpenAI OAuth accounts in Sub2API, retrying Codex OAuth, verifying replacement account IDs, and deleting only an explicitly confirmed old ID.

## Scenario: Sub2API 401 credential rotation

### 1. Scope / Trigger

Use this workflow when Sub2API manages OpenAI OAuth accounts and an operator needs to rotate accounts whose native batch refresh reports unauthorized/401. The workflow is verification-first: it snapshots stable Sub2API `account_id` values before retry, re-lists and refreshes accounts after retry, requires one healthy same-email replacement ID, and never treats local OAuth completion alone as proof of replacement.

CPA OAuth compatibility, ordinary Codex registration, Sub2API Agent Token upload, local export, scheduler policy, and quota policy are outside this contract.

### 2. Signatures

Management adapter:

```python
Sub2ApiAdminClient.list_openai_oauth_accounts(*, page_size: int = 100, max_pages: int = 100) -> list[dict]
Sub2ApiAdminClient.batch_refresh(account_ids: list[str]) -> dict
Sub2ApiAdminClient.delete_account(account_id: str) -> dict
```

Rotation service:

```python
Sub2Api401Service.detect(local_status: dict[str, str] | None = None) -> dict
Sub2Api401Service.select_retryable(values: list[object], *, limit: int = 50) -> tuple[list[dict], list[dict]]
Sub2Api401Service.mark_retrying(account_ids: list[str], *, batch_id: str = "") -> None
Sub2Api401Service.mark_verification_error(account_ids: list[str], reason: str) -> None
Sub2Api401Service.cleanup(values: list[object], *, opt_in: bool = False, limit: int = 50) -> dict
```

Web API:

```text
GET  /api/sub2api-401/state
POST /api/sub2api-401/detect
POST /api/sub2api-401/retry    {"account_ids": [string, ...], "workers": 1..8}
POST /api/sub2api-401/cleanup  {"account_ids": [string, ...], "confirm_cleanup": boolean}
GET  /sub2api-401
```

Sub2API operations:

```text
GET    /api/v1/admin/accounts?platform=openai&type=oauth&page=<n>&page_size=<n>&lite=1
POST   /api/v1/admin/accounts/batch-refresh  {"account_ids": [integer|string, ...]}
PUT    /api/v1/admin/accounts/<exact account_id>  {"status": "inactive"}
DELETE /api/v1/admin/accounts/<exact account_id>
```

### 3. Contracts

Environment is reused from `config/sub2api.py`: `SUB2API_API_BASE`, `SUB2API_API_KEY` or `SUB2API_API_TOKEN`, `SUB2API_API_AUTH_HEADER`, `SUB2API_API_AUTH_SCHEME`, and `SUB2API_API_TIMEOUT`. Do not introduce a second management-secret configuration. The list adapter may retain only a bounded, validated non-sensitive `created_at` value; malformed, missing, or tied latest timestamps are not usable for keeper selection.

The list adapter accepts direct payloads and standard `{code, data}` envelopes, traverses bounded pagination, deduplicates by normalized `account_id`, and retains only `platform=openai` plus `type=oauth`. Numeric IDs remain strings in audit/UI state but are sent as integers on batch-refresh requests. Email candidates are normalized; missing or conflicting candidates set `email_ambiguous` and are ineligible for retry and cleanup.

`batch_refresh()` scans the input in order, normalizes and deduplicates exact IDs, and selects at most 500 unique IDs (duplicate raw values do not consume the cap). It then sends consecutive chunks of at most 10 IDs serially to the same native batch-refresh endpoint. The chunk size bounds each request's latency under the existing Sub2API management timeout; chunks are intentionally not parallelized. Numeric IDs retain the existing integer wire representation. The logical `total` is exactly the number of selected IDs. For each chunk, reported `success` and `failed` values are independently clamped to the chunk size; explicit unique in-scope error IDs always count as failures, and success is reduced so that chunk success plus failure never exceeds that chunk's size. Missing success remains zero/conservative. The normalized counts are then summed, so `success` and `failed` remain within `0..total` and cannot exceed the selected population. In-scope errors are deduplicated by exact account ID in first-occurrence order and capped at 500; warnings retain chunk order and are capped at 50. If any chunk raises, processing stops immediately and the overall call raises without returning a partial result, preserving the service's existing snapshot and mutation safety behavior.

The `/sub2api-401` page must poll `GET /api/sub2api-401/state` every 3000 ms while any persisted item is `retrying`. The initial state load must start the same poll when the page is reopened during an in-flight retry, and a successful state response with no `retrying` items must cancel the pending timer. There must be at most one in-flight state request; polling only reads `state` and never invokes the expensive `/detect` endpoint. A transient poll error exposes only a bounded message and schedules a later retry while retrying work remains. The page applies server rows/counts/reasons on each successful response so background progress is visible without a manual reload.

`delete_account(account_id)` is the durable exact-ID cleanup primitive. It normalizes and URL-encodes the ID once, sends `PUT {"status": "inactive"}` to that exact account path, and sends DELETE to the same path only after the PUT succeeds. Both server response payloads are discarded. A deactivation failure short-circuits DELETE; a later DELETE failure remains safe to retry because the same exact ID may be deactivated again before deletion.

Batch refresh returns only bounded operational fields:

```text
total, success, failed, errors[{account_id, error}], warnings
```

Errors matching HTTP 401, `unauthorized`, `invalid_grant`, or `invalid_token` are unauthorized; timeouts are timeout; all remaining failures are unknown. Successful non-error IDs are healthy only when aggregate success counts confirm them.

Audit entries contain non-sensitive evidence only:

```text
account_id, account_name, email, created_at, state, health, reason,
observed_account_ids, pre_retry_account_ids, post_retry_account_ids,
replacement_account_id, replacement_health, cleanup_account_id, candidate_count,
checked_at, pre_retry_snapshot_at, post_retry_snapshot_at,
retry_started_at, retry_batch_id, local_codex_status,
retryable, cleanup_supported, manual_action
```

Allowed states are `pending`, `retrying`, `verified`, `cleanup_ready`, `cleaned`, `failed`, and `ambiguous`. `replacement_health` is empty without a keeper and otherwise records only the cleanup invariant `healthy` or `unauthorized`; audits created before this field existed default to `healthy` so deployed replacement cleanup remains restart-safe. Management keys, access/refresh tokens, OAuth codes, callback URLs, cookies, and credential JSON must never be logged, persisted, or returned. Reasons are bounded and secret-like text is redacted.

`sub2api_401_audit.json` is persisted atomically. If the configured audit path is a symlink, create the temporary file beside the resolved target so `os.replace()` remains on the writable data volume and preserves the symlink. Use mode `0600` where supported.

All Sub2API-401 responses set `Cache-Control: no-store, max-age=0`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff`.

### 4. Validation & Error Matrix

| Condition | Required result |
| --- | --- |
| Request body is not a JSON object | HTTP 400, bounded JSON error |
| `account_ids` is empty, not an array, or over 50 | HTTP 400, no account action |
| `workers` is not an integer | HTTP 400; otherwise clamp to `1..8` |
| No selected ID is retryable | HTTP 422 with bounded `skipped` entries |
| Live IDs differ from the detection snapshot | HTTP 409; mark affected ID ambiguous; do not start OAuth |
| Sub2API management returns 401/403 | HTTP 502; preserve old ID |
| Sub2API request times out | HTTP 504; preserve old ID |
| Required management route is absent | HTTP 501; no fallback deletion |
| Email is missing or maps to multiple account IDs | `ambiguous`; retry and cleanup disabled |
| OAuth/local retry fails | `failed`; preserve the old ID and snapshot |
| No new ID or multiple new IDs appear | `ambiguous`; cleanup disabled |
| Exactly one new same-email ID refreshes healthy and old ID remains | `cleanup_ready` with exact old/new IDs locked and `replacement_health=healthy`; the retry runner immediately performs exact-ID cleanup |
| Every member of a historical same-email duplicate group is unauthorized and one account has the unique latest valid `created_at` | Omit the latest keeper; mark each older exact ID `cleanup_ready` with `replacement_health=unauthorized`; detection performs no deletion |
| Historical all-unauthorized group has missing/invalid/tied `created_at`, or any timeout/unknown classification | `ambiguous`; no cleanup target is locked |
| `confirm_cleanup` is not literal `true` | Review only; no PUT or DELETE request |
| Locked state changes before cleanup | Skip stale target; no PUT or DELETE request |
| Historical unauthorized keeper cleanup precheck changes the group snapshot, any classification, email, persisted non-keeper `created_at`, or unique-latest result | `ambiguous`; no PUT or DELETE request |
| Exact old-ID deactivation fails | Adapter raises a sanitized management error; service records a bounded reason, sends no DELETE, and keeps `cleanup_ready` evidence for retry |
| Exact old-ID DELETE fails after deactivation | Keep `cleanup_ready` with bounded failure evidence; retry repeats PUT `inactive` before DELETE |
| Delete succeeds but old ID remains on re-list | Keep evidence and report verification failure |
| Delete/reconciliation confirms old ID absent but the replacement is missing or no longer matches its locked `healthy`/`unauthorized` invariant | `ambiguous`; keep bounded evidence for review |
| Re-list confirms old ID absent and a second refresh confirms the replacement healthy | `cleaned` |
| Re-list confirms an exact historical old ID absent, the keeper remains same-email and unique latest, and a second refresh confirms it unauthorized | `cleaned`; on the next detect the unauthorized singleton keeper becomes normal retryable `pending` |
| Historical unauthorized old ID is absent but the keeper no longer refreshes unauthorized or is no longer unique latest | `ambiguous`; retain the exact cleanup and keeper evidence |
| A retry row's locked old ID is absent on a later detect and no local Codex retry is running | Release its same-email IDs to normal per-account detection; keep the row only when it is `cleaned`, otherwise drop it. A revoked replacement becomes its own retryable `pending` row |
| A retry row's locked old ID is absent while local Codex retry is `retrying`/`queued`/`running` | Keep the row and its same-email ID lock until the retry finishes |

### 5. Good/Base/Bad Cases

- Good: old ID `42` is unauthorized, its same-email ID set is snapshotted, OAuth creates healthy ID `84`, and ID `42` still exists. State becomes `cleanup_ready`; the retry runner sends PUT `status=inactive` and then DELETE only to locked ID `42`, then re-list confirms it is absent and state becomes `cleaned`.
- Historical duplicate: one healthy account with unauthorized peers, multiple healthy accounts with one unique latest valid `created_at`, or all unauthorized accounts with one unique latest valid `created_at`, marks each older exact ID `cleanup_ready`; detection itself never deletes. The all-unauthorized branch refreshes the complete live same-email group before deletion, requires every classification to remain unauthorized, and refreshes the unique-latest keeper again after deletion.
- Base: healthy accounts are counted but omitted from the actionable list. Unauthorized accounts remain `pending` until the operator selects their exact IDs. The page defaults retry workers to 3; cleanup is a manual action only for historical duplicates or an automatic cleanup failure.
- Bad: email extraction is ambiguous, the pre-retry ID set changes, OAuth creates zero or multiple candidates, the new ID is not healthy, historical timestamps are missing/invalid/tied, historical health includes timeout/unknown, cleanup state/classification changes, or exact-ID deactivation fails. State becomes `failed`, `ambiguous`, or remains retryable `cleanup_ready`; DELETE is never sent after a failed deactivation.

### 6. Tests Required

- Adapter tests assert direct and enveloped responses, bounded pagination, repeated-page protection, OpenAI OAuth filtering, deterministic serial batch-refresh chunking (`10/10/5` for 25 IDs), duplicate-error deduplication and first-occurrence order, unique-ID cap behavior, independently normalized over-reported/missing success and failed counts, global `success`/`failed` bounds, aggregate warnings, numeric wire IDs, chunk-exception short-circuiting, exact encoded account paths, PUT `inactive` then DELETE call order/body, deactivation failure short-circuiting, safe error categories, discarded response payloads, and redaction.
- Service tests assert health classification, persisted snapshots, unique replacement, zero/multiple candidates, missing old IDs, local retry failure, email ambiguity, stale-snapshot conflict, concurrent detect/cleanup serialization, explicit cleanup opt-in, delete verification, safe deactivation/delete failure evidence retention, repeated deactivation on retry, and historical all-unauthorized unique-latest selection/refusal, classification drift, post-delete keeper drift, exact-ID deletion, and next-detect singleton retryability.
- Persistence tests assert atomic writes, resolved symlink target directories, preserved symlinks, mode `0600`, and no secret text.
- API tests assert authentication, request validation, HTTP 409/422 behavior, no-store headers, worker release on failures, exact-ID selection, and remote error mapping.
- Template tests assert modern/legacy navigation parity, all four Sub2API-401 endpoints, account ID evidence, explicit cleanup wording, no CPA-401 links, and a 3000 ms single-flight state poll that starts for persisted `retrying` rows and stops at terminal state.
- Quality gates: `py_compile`, focused `unittest`, extracted inline scripts with `node --check`, `git diff --check`, and related Codex/Sub2API regressions.

### 7. Wrong vs Correct

#### Wrong

```python
if local_codex_status == "success":
    delete_accounts_for_email(email)
```

Local completion is not remote proof, and email-wide deletion can remove the replacement or an unrelated account.

#### Correct

```python
service.mark_retrying([old_account_id], batch_id=batch_id)
run_existing_codex_retry(email)
result = service.detect({email: local_codex_status})
# The retry runner auto-cleans only the selected, locked IDs in cleanup_ready.
service.cleanup([old_account_id], opt_in=True)
```

Cleanup becomes available only after Sub2API shows one healthy new same-email `account_id` while the snapshotted old ID still exists. The delete target remains the exact locked old ID.
