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
DELETE /api/v1/admin/accounts/<exact account_id>
```

### 3. Contracts

Environment is reused from `config/sub2api.py`: `SUB2API_API_BASE`, `SUB2API_API_KEY` or `SUB2API_API_TOKEN`, `SUB2API_API_AUTH_HEADER`, `SUB2API_API_AUTH_SCHEME`, and `SUB2API_API_TIMEOUT`. Do not introduce a second management-secret configuration.

The list adapter accepts direct payloads and standard `{code, data}` envelopes, traverses bounded pagination, deduplicates by normalized `account_id`, and retains only `platform=openai` plus `type=oauth`. Numeric IDs remain strings in audit/UI state but are sent as integers on batch-refresh requests. Email candidates are normalized; missing or conflicting candidates set `email_ambiguous` and are ineligible for retry and cleanup.

Batch refresh returns only bounded operational fields:

```text
total, success, failed, errors[{account_id, error}], warnings
```

Errors matching HTTP 401, `unauthorized`, `invalid_grant`, or `invalid_token` are unauthorized; timeouts are timeout; all remaining failures are unknown. Successful non-error IDs are healthy only when aggregate success counts confirm them.

Audit entries contain non-sensitive evidence only:

```text
account_id, account_name, email, state, health, reason,
observed_account_ids, pre_retry_account_ids, post_retry_account_ids,
replacement_account_id, cleanup_account_id, candidate_count,
checked_at, pre_retry_snapshot_at, post_retry_snapshot_at,
retry_started_at, retry_batch_id, local_codex_status,
retryable, cleanup_supported, manual_action
```

Allowed states are `pending`, `retrying`, `verified`, `cleanup_ready`, `cleaned`, `failed`, and `ambiguous`. Management keys, access/refresh tokens, OAuth codes, callback URLs, cookies, and credential JSON must never be logged, persisted, or returned. Reasons are bounded and secret-like text is redacted.

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
| Exactly one new same-email ID refreshes healthy and old ID remains | `cleanup_ready` with exact old/new IDs locked |
| `confirm_cleanup` is not literal `true` | Review only; no DELETE request |
| Locked state changes before cleanup | Skip stale target; no DELETE request |
| Delete succeeds but old ID remains on re-list | Keep evidence and report verification failure |
| Delete and re-list confirm old ID absent | `cleaned` |

### 5. Good/Base/Bad Cases

- Good: old ID `42` is unauthorized, its same-email ID set is snapshotted, OAuth creates healthy ID `84`, and ID `42` still exists. State becomes `cleanup_ready`; explicit confirmation deletes only `42`, then re-list confirms it is absent.
- Base: healthy accounts are counted but omitted from the actionable list. Unauthorized accounts remain `pending` until the operator selects their exact IDs.
- Bad: email extraction is ambiguous, the pre-retry ID set changes, OAuth creates zero or multiple candidates, the new ID is not healthy, or cleanup state becomes stale. State becomes `failed` or `ambiguous`; no deletion occurs.

### 6. Tests Required

- Adapter tests assert direct and enveloped responses, bounded pagination, repeated-page protection, OpenAI OAuth filtering, numeric wire IDs, exact DELETE paths, safe error categories, and redaction.
- Service tests assert health classification, persisted snapshots, unique replacement, zero/multiple candidates, missing old IDs, local retry failure, email ambiguity, stale-snapshot conflict, concurrent detect/cleanup serialization, explicit cleanup opt-in, delete verification, and failed cleanup evidence retention.
- Persistence tests assert atomic writes, resolved symlink target directories, preserved symlinks, mode `0600`, and no secret text.
- API tests assert authentication, request validation, HTTP 409/422 behavior, no-store headers, worker release on failures, exact-ID selection, and remote error mapping.
- Template tests assert modern/legacy navigation parity, all four Sub2API-401 endpoints, account ID evidence, explicit cleanup wording, and no CPA-401 links.
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
service.cleanup([old_account_id], opt_in=operator_confirmed)
```

Cleanup becomes available only after Sub2API shows one healthy new same-email `account_id` while the snapshotted old ID still exists. The delete target remains the exact locked old ID.
