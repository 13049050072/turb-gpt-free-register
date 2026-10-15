# ChatGPT2API Credential Rotation

> Executable contract for detecting expired ChatGPT2API Web-pool access tokens, queueing the normal local liveness flow, and automatically replacing only each locked remote account ID after full evidence validation.

## Scenario: ChatGPT2API 401 queued AT rotation

### 1. Scope / Trigger

Use this workflow when ChatGPT2API reports a Web account access token as explicitly invalid, expired, unauthorized, or HTTP 401. Detection locks the stable remote ID, unique email mapping, local account ID, old AT digest, and refresh-token preservation digest. A confirmed request queues liveness work in the application process and returns immediately; each successful liveness result is automatically written back to the exact locked remote ID.

This contract assumes the deployed topology is one Gunicorn process with threaded request handling. A multi-process or distributed queue is out of scope. Sub2API rotation, ordinary registration, ordinary liveness pages, ChatGPT2API import, CPA compatibility, pool scheduling, and quota policy are unaffected.

### 2. Signatures

Management adapter:

```python
ChatGPT2APIAdminClient.list_accounts(*, page_size: int = 500, max_pages: int = 20) -> list[dict]
ChatGPT2APIAdminClient.credential_snapshot(remote_id: str) -> dict
ChatGPT2APIAdminClient.patch_access_token(remote_id: str, access_token: str) -> dict
ChatGPT2APIAdminClient.delete_account(remote_id: str, *, wait_seconds: float = 15.0, poll_interval: float = 0.5) -> dict
```

Liveness adapter and service:

```python
run_account_live_check(account_id: int, *, trigger: str = "chatgpt2api-401", proxy: str | None = None) -> dict
run_liveness_check(account_id: int) -> dict
ChatGPT2API401Service.detect() -> dict
ChatGPT2API401Service.select_refreshable(values: object, *, limit: int = 5000) -> tuple[list[dict], list[dict]]
ChatGPT2API401Service.refresh(values: object, *, opt_in: bool = False, limit: int = 5000) -> dict
ChatGPT2API401Service.replace(values: object, *, opt_in: bool = False, limit: int = 5000) -> dict
```

Queue constants:

```python
CHATGPT2API_401_MAX_ITEMS = 5000
CHATGPT2API_401_WORKERS = 5
```

Web API:

```text
GET  /chatgpt2api-401
GET  /api/chatgpt2api-401/state
POST /api/chatgpt2api-401/detect
POST /api/chatgpt2api-401/refresh  {"remote_ids": [string, ...], "confirm_replace": true}  -> HTTP 202
POST /api/chatgpt2api-401/replace  {"remote_ids": [string, ...], "confirm_replace": boolean}
```

The replace endpoint remains available for historical `ready` audit entries. New confirmed refresh jobs automatically call the same exact-ID replacement path.

ChatGPT2API operations:

```text
GET  /api/accounts?page=<n>&page_size=<n>
GET  /api/accounts/{account_id}/access-token
GET  /api/accounts/{account_id}/refresh-token
POST /api/accounts/update  {"id": <exact remote ID>, "access_token": <new AT>}
DELETE /api/accounts        {"account_ids": [<exact remote ID>]}
GET  /api/accounts/{account_id}   # 404 confirms deletion
```

### 3. Queue and State Contract

`POST /api/chatgpt2api-401/refresh` accepts 1 through 5000 remote IDs and requires `confirm_replace is True`. A valid request persists all accepted entries as `queued`, submits them to a process-local `ThreadPoolExecutor(max_workers=5)`, and returns HTTP 202 without waiting for any liveness call. The in-process outstanding queue is bounded to 5000 jobs.

Only `pending` and retryable `failed` entries may be queued. `queued`, `refreshing`, `ready`, `replaced`, `deleted`, `ambiguous`, and `conflict` entries cannot be submitted again. Duplicate and non-actionable IDs are returned in a bounded `skipped` array without credentials. Detection returns HTTP 409 while any item is `queued` or `refreshing`, so it cannot replace active audit entries.

The persisted state machine is:

```text
pending -> queued -> refreshing -> ready -> replaced
             |           |           |
             |           v           v
             |         failed      conflict
             |           |
             |           +-- liveness status=deactivated --+
             +-- local live_check_status=deactivated ------+--> deleted
                                                           |
                                                           +--> conflict (pre_delete) / failed (delete, retryable)

pending/failed -- pre-liveness snapshot drift --> conflict
queued/refreshing -- process restart ----------> failed (retryable)
```

`ready` is normally transient and exists so automatic writeback uses the established replacement gate. The ready transition and automatic `replace()` call remain under the same re-entrant operation lock. Historical ready entries remain manually replaceable.

`state()` returns only safe fields plus:

```json
{
  "progress": {
    "workers": 5,
    "queued": 0,
    "active": 0,
    "completed": 0,  # replaced + deleted
    "failed": 0
  }
}
```

The UI polls every 3000 ms while `queued + active > 0`. Reloading or closing the page does not cancel jobs; reopening reads persisted progress.

### 4. Evidence and Writeback Contract

Reuse `config/chatgpt2api.py`: `CHATGPT2API_BASE_URL`, `CHATGPT2API_MANAGEMENT_KEY`, and `CHATGPT2API_REQUEST_TIMEOUT`. Management calls use `Authorization: Bearer <key>` and never log headers or response bodies.

Safe mapping requires exactly one remote row for the stable remote ID, exactly one remote row for the normalized email, and exactly one local account for that email. Email is a join key only and is never the mutation target.

Detection stores hashes only:

```text
old_at_sha256
preserved_sha256[refresh_token]  # hash({present: bool, value: string})
```

A 404 from the RT endpoint is explicit absence and still hashes to non-empty evidence. HTTP 200 without a non-empty RT is unverifiable. Password, client ID, last-refresh value, and ID token are neither guessed nor claimed as verified.

Before liveness, a worker re-lists both sides and verifies the locked remote ID/email uniqueness, local ID/email uniqueness, old AT hash, and RT preservation hash. Any drift becomes `conflict` at `pre_liveness`; liveness and remote mutation are skipped.

Liveness calls `run_liveness_check(local_account_id)`, which delegates to `run_account_live_check()` and therefore retains the normal DB claim, routing, proxy fallback, log, refresh, and persistence behavior. The five queue workers may run liveness concurrently. Success requires all of the following:

- liveness returns `ok is True` and a non-empty AT;
- returned AT hash equals the local persisted AT hash;
- new hash differs from the locked remote old AT hash;
- new hash differs from the pre-liveness local AT hash.

Failures become retryable `failed` at stage `liveness` and never write remotely.

After liveness, remote writeback is serialized by the shared audit-path operation lock. `replace()` revalidates both mappings, old AT evidence, RT evidence, and the persisted new AT. `POST /api/accounts/update` sends exactly `{id, access_token}`. The adapter accepts success only when `updated_ids` contains the exact locked ID, then re-fetches AT and RT. The service also re-lists and verifies that the same ID/email remains unique, AT equals the locked new hash and differs from old, and RT evidence is unchanged. Only then does state become `replaced`.

Writeback acknowledgement mismatch, exceptions, or post-write evidence mismatch become `conflict` at stage `writeback`. The system never claims success from the mutation response alone.

### 4.1 Deactivated Account Deletion

A deactivated account cannot be revived by a new AT, so the locked remote ID is deleted instead of being retried forever. Two triggers exist, both under the shared operation lock:

- the pre-liveness local row already has `live_check_status == "deactivated"` (a persisted verdict from an earlier liveness or Codex check): liveness is skipped;
- liveness returns `status == "deactivated"` (for example `account_deactivated`), which `update_account_liveness` persists locally.

Before deleting, the service re-lists both sides and requires the same unique remote ID/email and local ID/email mapping, the local row still `deactivated`, and the remote AT hash and RT evidence still equal to the detection lock. Any drift becomes `conflict` at stage `pre_delete` and nothing is deleted, so an account rotated elsewhere is never removed.

`DELETE /api/accounts` sends exactly `{"account_ids": [id]}`. The adapter requires the exact ID in `target_ids`, then polls `GET /api/accounts/{id}` until 404 (upstream deletion is a background operation). The service re-lists once more and requires the ID to be absent. Only then does state become `deleted` with `deleted_at`. Delete errors, unconfirmed targets, or a lingering row become retryable `failed` at stage `delete`; the retry reuses the persisted local verdict and does not log in again. Non-deactivated liveness failures never delete.

### 5. Persistence and Confidentiality

`chatgpt2api_401_audit.json` uses create-exclusive temporary files, `fsync`, `os.replace`, and mode `0600` where supported. A symlink audit path writes beside the resolved target and preserves the symlink. Service instances sharing an audit path share one process-level `RLock` and reload persisted state before mutations.

On service initialization, persisted `queued` and `refreshing` entries are atomically converted to retryable `failed` entries with failure stage `restart`. Work is not silently resumed because the in-memory executor does not survive a process restart.

Public state and audit data contain bounded remote IDs, normalized emails, local IDs, states, bounded redacted reasons, failure stages, timestamps, booleans, counts, and 12-character AT fingerprints. They never contain AT, RT, management keys, passwords, OAuth codes, callback URLs, authorization headers, or complete upstream response bodies.

All ChatGPT2API-401 responses set `Cache-Control: no-store, max-age=0`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff`.

### 6. Validation and Error Matrix

| Condition | Required result |
| --- | --- |
| Request body is not a JSON object | HTTP 400, no action |
| `remote_ids` is empty, malformed, or over 5000 | HTTP 400, no action |
| Refresh confirmation is not literal `true` | HTTP 400, no queue or writeback |
| Valid refresh request | HTTP 202 before liveness completes |
| Duplicate/non-actionable ID | Bounded skipped reason, no duplicate job |
| Detection while queue is active | HTTP 409, active audit preserved |
| Remote ID/email or local ID/email drift before liveness | `conflict`, no liveness |
| Old AT or RT evidence drifts before liveness | `conflict`, no liveness |
| Liveness fails or returned/persisted hashes disagree | retryable `failed`, no writeback |
| New AT equals old remote AT or prior local AT | retryable `failed`, no writeback |
| Mapping or evidence changes before update | `conflict`, no update request |
| `updated_ids` omits the locked remote ID | `conflict`, never success |
| Post-update ID, email, AT, or RT evidence differs | `conflict`, retain safe audit evidence |
| Post-update evidence matches | `replaced` |
| Process starts with queued/refreshing audit rows | retryable `failed`, stage `restart` |
| Liveness returns `deactivated`, evidence unchanged | exact-ID delete, 404 readback, `deleted` |
| Local row already `deactivated` | no liveness, exact-ID delete, `deleted` |
| Remote AT/RT or mapping drift before delete | `conflict`, stage `pre_delete`, no delete |
| Delete error, unconfirmed target, or row still listed | retryable `failed`, stage `delete` |
| Liveness fails without `deactivated` | retryable `failed`, never deletes |

### 7. Required Tests

- Service tests prove enqueue returns while liveness is blocked, 60 candidates are accepted, exactly five liveness calls can be active, all candidates finish, duplicate enqueue is skipped, and active detection is rejected.
- Service tests cover old AT drift, RT drift, mapping drift, liveness failure, returned/persisted mismatch, unchanged local AT, patch failure, post-write RT change, exact-ID automatic replacement, RT preservation, restart recovery, and historical ready compatibility.
- API tests cover authentication, literal confirmation, HTTP 202, acceptance above 50, rejection above 5000, invalid element rejection, HTTP 409 while active, no-store headers, and bounded redacted errors.
- Adapter tests cover exact `{id, access_token}`, exact `updated_ids`, dedicated AT/RT reads, missing-RT hashing, and post-update evidence.
- Template tests cover full selection without 50-item truncation, automatic-writeback wording, fixed concurrency 5, 3000 ms polling, queued/refreshing states, and manual historical replacement.
- Regression gates cover ChatGPT2API import/connection, normal liveness, Sub2API 401, `py_compile`, extracted inline JavaScript `node --check`, `git diff --check`, and full `unittest` discovery.

### 8. Good / Base / Bad Cases

- **Good**: 500 invalid accounts are selected; the route persists accepted IDs, returns HTTP 202 immediately, five workers reuse normal liveness, and each successful account reaches `replaced` only after exact-ID AT readback and unchanged RT evidence.
- **Base**: one account fails liveness while the remaining accounts continue; that item becomes retryable `failed`, no remote mutation occurs for it, and queue progress reaches idle without losing other results.
- **Bad**: an implementation waits for liveness inside the HTTP request, truncates selection to 50, mutates by email, trusts `updated_ids` without readback, or writes remotely after AT/RT snapshot drift.

### 9. Wrong vs Correct

#### Wrong

```python
# Blocks the Cloudflare request and separates refresh from writeback.
for remote_id in remote_ids[:50]:
    service.refresh_one(remote_id)
return jsonify(service.state())
```

#### Correct

```python
# Explicit confirmation, bounded enqueue, immediate 202; workers reuse normal
# liveness and call the exact-ID replacement gate after evidence validation.
result = service.refresh(remote_ids, opt_in=(payload.get("confirm_replace") is True), limit=5000)
return jsonify(result), 202
```
