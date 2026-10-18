# Account Inspection

> Executable contract for one-click account inspection, durable progress, failure details, and stale-list prevention.

## Scenario: One-click account inspection

### 1. Scope / Trigger

Use this workflow when an operator starts an account inspection from the modern or Legacy account list. The service snapshots the selected archive scope, parses each stored access-token expiry locally, runs the existing liveness flow only for invalid or missing tokens, and reuses the same successful session to refresh plan data.

This workflow is distinct from the periodic Sub2API-401 and ChatGPT2API-401 schedulers. It does not delete accounts automatically. Account deletion from the inspection history is an explicit operator action against confirmed deactivated rows.

### 2. Signatures

Service:

```python
validate_workers(workers: int) -> int
start_inspection(workers: int = 3, archived: str | bool = "0") -> tuple[dict, bool]
list_inspections(limit: int = 50, offset: int = 0) -> dict
recover_interrupted_inspections() -> None
startup_recovery_guard() -> ContextManager[bool]
```

Persistence:

```python
create_account_inspection(account_ids: list[int] | None = None, workers: int = 3, archived: str = "0") -> tuple[dict, bool]
list_account_inspections(limit: int = 50, offset: int = 0) -> dict
list_account_inspection_failures(inspection_id: int, limit: int = 20, offset: int = 0) -> dict | None
redact_error(value: object) -> str
```

Web API:

```text
GET  /api/accounts/inspections?page=<positive integer>
POST /api/accounts/inspections
     {"workers": 1..32, "archived": "0" | "1" | "only" | boolean}
GET  /api/accounts/inspections/<signed integer inspection_id>/failures?page=<positive integer>
```

### 3. Contracts

`POST /api/accounts/inspections` returns HTTP 202 with `{ok: true, task}` only after the task and its immutable account-ID snapshot are durable. If another task is active, it returns HTTP 409 and preserves the existing task.

The persisted task states are `queued`, `running`, `completed`, `failed`, and `interrupted`. Progress and stage counters are committed incrementally. Startup recovery must mark orphaned work terminal and release stale account-activity claims. A process-shared lease prevents a second app instance from recovering a task still owned by the first instance.

The failure-detail table uses `(inspection_id, account_id)` as its composite primary key. The primary-key auto-index is authoritative; do not add a duplicate index with the same columns. Task summary reads (`items`, `active`, and `total`) and failure-detail reads (`task`, `items`, and `total`) each use one SQLite read transaction so one API response represents one snapshot.

Failure details expose only bounded operational fields: account ID, normalized email, stage, state, bounded reason, and timestamps. Before persistence and again before API output, `redact_error()` removes passwords, access/refresh/ID tokens, API keys, authorization values, OAuth codes and callbacks, cookies/sessions/JWTs, private keys, client secrets, and proxy credentials. Proxy redaction covers scheme URLs, labeled no-scheme values, and bounded bare `user:pass@host:port` values. Errors are capped at 500 characters. Production credentials must never appear in logs, fixtures, docs, task payloads, or responses.

Failure-detail responses always set:

```text
Cache-Control: no-store, max-age=0
Pragma: no-cache
X-Content-Type-Options: nosniff
```

The modern and Legacy account lists share the same request-coalescing contract: at most one list request may be in flight. Polls received while a request is active become one pending refresh, and each completed response may render before the pending request starts. Delete, archive, and restore mutations must call `loadAccounts({invalidate: true})` on both success and uncertain/error outcomes so a pre-mutation response cannot render stale accounts.

### 4. Validation & Error Matrix

| Condition | Required result |
| --- | --- |
| Request body is not a JSON object | HTTP 400; no task created |
| `workers` is bool, non-integer, below 1, or above 32 | HTTP 400; no task created |
| Archive mode is unsupported | HTTP 400; no task created |
| Another inspection is active | HTTP 409; existing task remains authoritative |
| Valid start | HTTP 202 after durable snapshot creation |
| List `page` is non-integer, non-positive, or offset-overflowing | HTTP 400 |
| Failure `inspection_id` is negative, zero, or above SQLite signed 64-bit maximum | HTTP 400 before database access, with all no-store headers |
| Valid positive inspection ID does not exist | HTTP 404 with all no-store headers |
| Failure-detail storage read raises | HTTP 503 with bounded generic error and all no-store headers |
| Persisted task was running when the process died | Recover as `interrupted`; preserve completed counters |
| A mutation response is lost after the server may have committed | Invalidate the current list generation and perform a fresh read |
| Error text contains credential-shaped material | Persist and return redacted bounded text only |

### 5. Good / Base / Bad Cases

- **Good**: 1,000 accounts are snapshotted. Locally valid JWTs skip login, invalid tokens reuse the normal liveness path, plan refresh uses the successful session, counters reach a terminal state, and failure rows are paginated without loading a task-wide JSON blob.
- **Base**: some accounts fail liveness. Other accounts continue; each failed row records only a redacted bounded reason, and no account is deleted automatically.
- **Bad**: a second task starts against a moving account list, an API response mixes counts from different SQLite snapshots, a timeout after archive allows an old in-flight list to repaint the archived account, or a proxy/password/token reaches SQLite or JSON.

### 6. Tests Required

- Service tests assert immutable account snapshots, archive scopes, worker validation, single-flight start, interruption recovery, account-activity release, incremental persistence, and stage counts.
- Database tests assert migration compatibility, rollback, process-shared leasing, one-snapshot reads, targeted failure-row updates, composite-primary-key lookup, and large-task performance.
- Confidentiality tests pass only obviously fake secrets through task errors and failure details, then inspect both SQLite and API output for redaction. Include Bearer/Basic, OAuth callback/code, token fields, scheme and no-scheme proxy credentials, and bare `user:pass@host:port` fixtures.
- API tests assert 202/400/404/409/503 behavior, signed 64-bit ID bounds, pagination bounds, authentication, and no-store/nosniff headers.
- Template tests cover modern and Legacy controls, accessible history/failure dialogs, one in-flight list request, pending poll coalescing, and invalidation for success and uncertain outcomes of delete/archive/restore.
- Quality gates include focused Python tests, both Node UI tests, inline JavaScript syntax checks, `py_compile`, `git diff --check`, and full discovery with any unrelated clean-HEAD failures reproduced explicitly.

### 7. Wrong vs Correct

#### Wrong

```python
# Stores upstream text verbatim and duplicates the composite PK index.
run["error"] = str(exc)
CREATE INDEX idx_failure ON account_inspection_failures(inspection_id, account_id)
```

```javascript
// A pre-mutation response can repaint deleted or archived rows.
await mutateAccounts();
loadAccounts();
```

#### Correct

```python
run["error"] = redact_error(exc)
# PRIMARY KEY (inspection_id, account_id) already supplies the lookup index.
```

```javascript
try {
  await mutateAccounts();
} finally {
  loadAccounts({invalidate: true});
}
```
