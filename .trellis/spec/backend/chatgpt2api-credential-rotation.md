# ChatGPT2API Credential Rotation

> Executable contract for detecting expired ChatGPT2API Web-pool access tokens, reusing the local liveness worker, and replacing only one locked remote account ID after full evidence validation.

## Scenario: ChatGPT2API 401 access-token rotation

### 1. Scope / Trigger

Use this workflow when a ChatGPT2API Web account has an explicitly invalid, expired, unauthorized, or HTTP 401 access token and the operator wants to refresh the matching local account, then update the same remote record. This is verification-first: detection locks a stable remote ID and credential hashes; liveness must return and persist a different AT; replacement requires explicit confirmation and verifies the remote record after update.

Sub2API account replacement/deletion, ordinary registration, ordinary queued liveness, ChatGPT2API import, CPA OAuth compatibility, pool scheduling, and quota policy are outside this contract.

### 2. Signatures

Management adapter:

```python
ChatGPT2APIAdminClient.list_accounts(*, page_size: int = 500, max_pages: int = 20) -> list[dict]
ChatGPT2APIAdminClient.credential_snapshot(remote_id: str) -> dict
ChatGPT2APIAdminClient.patch_access_token(remote_id: str, access_token: str) -> dict
```

Liveness adapter and service:

```python
run_account_live_check(account_id: int, *, trigger: str = "chatgpt2api-401", proxy: str | None = None) -> dict
run_liveness_check(account_id: int) -> dict
ChatGPT2API401Service.detect() -> dict
ChatGPT2API401Service.select_refreshable(values: object, *, limit: int = 50) -> tuple[list[dict], list[dict]]
ChatGPT2API401Service.refresh(values: object, *, limit: int = 50) -> dict
ChatGPT2API401Service.replace(values: object, *, opt_in: bool = False, limit: int = 50) -> dict
```

Web API:

```text
GET  /chatgpt2api-401
GET  /api/chatgpt2api-401/state
POST /api/chatgpt2api-401/detect
POST /api/chatgpt2api-401/refresh  {"remote_ids": [string, ...]}
POST /api/chatgpt2api-401/replace  {"remote_ids": [string, ...], "confirm_replace": boolean}
```

ChatGPT2API operations:

```text
GET  /api/accounts?page=<n>&page_size=<n>
GET  /api/accounts/{account_id}/access-token
GET  /api/accounts/{account_id}/refresh-token
POST /api/accounts/update  {"id": <exact remote ID>, "access_token": <new AT>}
```

### 3. Contracts

Reuse `config/chatgpt2api.py`: `CHATGPT2API_BASE_URL`, `CHATGPT2API_MANAGEMENT_KEY`, and `CHATGPT2API_REQUEST_TIMEOUT`. Do not introduce another management-secret configuration. Management calls use `Authorization: Bearer <key>` and never log headers or response bodies.

The list adapter accepts direct rows and common nested envelopes, traverses at most 20 pages with page size capped at 500, stops on repeated pages, and preserves rows with missing IDs so they can block unsafe uniqueness assumptions. A candidate is invalid only when an explicit status or reason matches invalid, expired, unauthorized, HTTP 401, invalid access token, or token expired.

Safe mapping requires exactly one remote row for the stable remote ID, exactly one remote row for the normalized email, and exactly one local account for that email. The email is a join key only; it is never the remote mutation target.

`GET /api/accounts` is sanitized and never contains credential values. Detection verifies remote-ID and email uniqueness from that list, then fetches the old AT from `/access-token` and the RT from `/refresh-token`, retaining only SHA-256 evidence:

```text
old_at_sha256
preserved_sha256[refresh_token]  # hash({present: bool, value: string})
```

A 404 from the RT endpoint is an explicit `{present:false,value:""}` state and still produces a non-empty digest. HTTP 200 without a non-empty `refresh_token` is unverifiable and blocks the workflow as a conflict. The service contract is `_PRESERVED_FIELDS = ('refresh_token',)`. `password`, `client_id`, `last_refresh`, and `id_token` are not guessed, and no ID-token preservation claim is made. The public state contains only bounded remote ID, normalized email, local account ID, state, reason, timestamps, booleans, and 12-character AT fingerprints.

`refresh()` reuses `run_account_live_check()` and the normal DB claim, routing, proxy fallback, logging, and `update_account_liveness()` behavior. Success requires the returned AT hash to equal the persisted local AT hash, differ from the locked remote AT hash, and differ from the pre-liveness local AT hash.

`replace()` is a preview unless `confirm_replace is True`. Immediately before update it revalidates remote ID/email uniqueness, local ID/email uniqueness, old AT hash, the RT presence/value hash, and the persisted new AT hash. `POST /api/accounts/update` sends the exact body `{id, access_token}` and is successful only when direct or enveloped `updated_ids` includes the exact locked remote ID. After acknowledgement, the adapter must re-fetch both `/access-token` and `/refresh-token` and verify their hashes; the service also re-lists and verifies the ID still exists uniquely, the AT hash equals the new hash and differs from the old hash, and the RT presence/value hash is unchanged.

`chatgpt2api_401_audit.json` is written with create-exclusive temporary files, `fsync`, `os.replace`, and mode `0600` where supported. For a symlink audit path, create the temporary file beside the resolved target and preserve the symlink. Operations sharing an audit path use one process-level operation lock and reload persisted state before mutations.

All ChatGPT2API-401 responses set `Cache-Control: no-store, max-age=0`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff`.

### 4. Validation & Error Matrix

| Condition | Required result |
| --- | --- |
| Request body is not a JSON object | HTTP 400, bounded error, no action |
| `remote_ids` is empty, not an array, or over 50 | HTTP 400, no action |
| Management configuration is incomplete | HTTP 503, no remote action |
| Management authentication fails | HTTP 502, no credential echo |
| Management request times out | HTTP 504, preserve locked state |
| Account/update route is unsupported | HTTP 501, no fallback mutation |
| Invalid signal is absent | Count as healthy/non-actionable |
| Remote ID is missing or duplicated | `ambiguous`, refresh/replace disabled |
| Email is missing, conflicting, or maps to multiple remote/local rows | `ambiguous`, refresh/replace disabled |
| Credential snapshot lacks old AT or RT presence/value digest | `ambiguous`, no liveness |
| Mapping or snapshot changes before liveness | `conflict`, no liveness call |
| Liveness fails or returned/persisted ATs disagree | `failed`, retry allowed, no remote update |
| New AT equals old remote AT or pre-liveness local AT | `failed`, no remote update |
| `confirm_replace` is not literal `true` | Preview only, no update request |
| Mapping, old AT, RT presence/value, or new AT changes before update | `conflict`, no update request |
| `updated_ids` omits the locked ID | `conflict`, update not accepted |
| Post-update ID, email, AT, or RT evidence differs | `conflict`, retain audit evidence |
| Post-update evidence matches | `replaced` |

### 5. Good/Base/Bad Cases

- Good: remote ID `web-42` has explicit 401 state and uniquely maps to local account `17`. Detection locks the old AT hash and RT presence/value hash; liveness returns and persists a different AT; explicit confirmation updates only `web-42`; post-update AT/RT reads and the sanitized re-list confirm the same ID and email, new AT hash, and unchanged RT evidence. State becomes `replaced`.
- Base: healthy remote rows are counted but are not actionable. Invalid rows remain `pending` until the operator selects their exact remote IDs.
- Bad: a duplicate email exists, a snapshot field is missing, liveness returns the old token, a second instance changes audit state, or the remote update response confirms a different ID. No remote replacement is accepted; state remains `ambiguous`, `failed`, or `conflict`.

### 6. Tests Required

- Adapter tests assert direct/enveloped sanitized lists, bounded pagination, repeated-page protection, explicit invalid classification, duplicate/missing ID retention, dedicated AT/RT secret fetches, explicit missing-RT hashing, hash-only snapshots, exact `{id, access_token}` update payload, exact `updated_ids` confirmation, post-update AT/RT verification, and secret-redacted errors.
- Service tests assert unique remote/local mapping, old and preserved hashes, pending→refreshing→ready→replaced, returned-versus-persisted AT equality, old/pre-local AT inequality, stale mapping/snapshot rejection, explicit confirmation preview, post-update verification, interrupted refresh recovery, and shared-audit-path serialization.
- Persistence tests assert atomic writes, resolved symlink target directories, preserved symlinks, mode `0600`, and absence of AT/RT/password/key/code/callback text.
- API tests assert authentication, 50-ID request bounds, literal boolean confirmation, no-store headers, safe HTTP category mapping, and no credential output.
- Template tests assert modern/legacy navigation, all four endpoints, stable ID and fingerprint evidence, explicit confirmation wording, and no embedded secrets.
- Regression gates cover ChatGPT2API import/connection, normal liveness, Sub2API 401, `py_compile`, extracted inline JavaScript `node --check`, `git diff --check`, and full `unittest` discovery with baseline-failure comparison.

### 7. Wrong vs Correct

#### Wrong

```python
if remote_email == local_email:
    update_all_accounts_for_email(email, new_access_token)
```

Email is not a stable mutation identity, and liveness success alone does not prove that the returned token was persisted, changed, or belongs to the locked remote snapshot.

#### Correct

```python
state = service.detect()
service.refresh([locked_remote_id])
preview = service.replace([locked_remote_id], opt_in=False)
result = service.replace([locked_remote_id], opt_in=operator_confirmed)
```

The service updates only the exact locked remote ID after proving unique mappings, the old AT hash, the RT presence/value hash, a newly returned-and-persisted AT, literal confirmation, exact `updated_ids`, and matching post-update AT/RT evidence.
