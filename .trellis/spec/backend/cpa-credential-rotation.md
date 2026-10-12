# CPA Credential Rotation

> Executable contract for verification-first rotation of CPA Codex credentials that return unauthorized/401.

## Scenario: CPA 401 credential rotation

### 1. Scope / Trigger

Use this workflow when the CPA management API reports a Codex credential as unauthorized/401 and an operator explicitly selects that account for OAuth retry. The workflow is non-destructive by default: it persists exact filenames before retry, re-reads CPA after retry, verifies one replacement, and exposes old filenames for review. Local Codex success alone is never proof of CPA replacement.

### 2. Signatures

Backend service:

```python
Cpa401Service.detect(local_status: dict[str, str] | None = None) -> dict
Cpa401Service.select_retryable(values: list[object], *, limit: int = 50) -> tuple[list[dict], list[dict]]
Cpa401Service.mark_retrying(emails: list[str], *, batch_id: str = "") -> None
Cpa401Service.mark_verification_error(emails: list[str], reason: str) -> None
Cpa401Service.cleanup(values: list[object], *, opt_in: bool = False, limit: int = 50) -> dict
```

Web API:

```text
GET  /api/cpa-401/state
POST /api/cpa-401/detect
POST /api/cpa-401/retry    {"emails": [string, ...], "workers": 1..8}
POST /api/cpa-401/cleanup  {"emails": [string, ...], "confirm_cleanup": boolean}
GET  /cpa-401
```

CPA read/verification operations:

```text
POST /v8/management/credentials/refresh  {"all": true}
GET  /v0/management/auth-files
GET  /v0/management/auth-files/download?name=<exact filename>
```

No CPA deletion operation is part of the default contract.

### 3. Contracts

Requests:

- `emails` must be a non-empty JSON array with at most 50 entries.
- Emails are normalized with trim plus lowercase and deduplicated before use.
- `workers` is clamped to `1..8` after integer validation.
- `confirm_cleanup=true` is meaningful only when a verified cleanup adapter is explicitly configured.

State entries contain only non-sensitive operational evidence:

```text
email, state, reason, files, pre_retry_files, post_retry_files,
cleanup_files, replacement_file, candidate_count, checked_at,
pre_retry_snapshot_at, post_retry_snapshot_at, retry_started_at,
retry_batch_id, local_codex_status, retryable, cleanup_supported,
manual_action
```

Allowed states are `pending`, `retrying`, `verified`, `cleanup_ready`, `cleaned`, `failed`, and `ambiguous`. Reasons are bounded to 180 characters and secret-like values are redacted. Filenames are bounded, deduplicated, and used as exact identities; credential JSON, tokens, callback values, passwords, cookies, and management keys are never persisted or returned.

The audit file is written atomically to `/app/cpa_401_audit.json`, which the container entrypoint links to `/data/cpa_401_audit.json`. Temporary files use mode `0600` where supported.

All CPA-401 responses set `Cache-Control: no-store, max-age=0`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff`.

### 4. Validation & Error Matrix

| Condition | Required result |
| --- | --- |
| Request body is not a JSON object | HTTP 400, bounded JSON error |
| `emails` is empty, not an array, or over 50 | HTTP 400, no account action |
| No selected item can be retried | HTTP 422 with bounded `skipped` entries |
| Live CPA filenames changed since detection | HTTP 409; mark affected item ambiguous; do not start OAuth |
| CPA management returns 401/403 | HTTP 502; preserve old files |
| CPA request times out | HTTP 504; preserve old files |
| Refresh endpoint returns 404/unsupported | HTTP 501; no destructive fallback |
| OAuth/local retry fails | `failed`; preserve `pre_retry_files` |
| No replacement or multiple candidates exist | `ambiguous`; cleanup disabled |
| One new healthy file passes exact download validation | `cleanup_ready` when old files remain, otherwise `verified` |
| CPA overwrites one existing filename and it verifies | `verified`; no old-file cleanup queue |
| Cleanup adapter is absent | HTTP 200 review-only result with `cleanup_supported=false` |
| Per-file cleanup fails with a configured adapter | Preserve remaining exact filenames and report bounded per-file result |

### 5. Good/Base/Bad Cases

- Good: one old unauthorized filename is snapshotted, OAuth completes, exactly one different healthy filename appears, and its downloaded JSON matches the normalized account. The item becomes `cleanup_ready`; the old exact filename is reviewable but is not deleted by default.
- Base: CPA overwrites the same filename. A healthy status plus exact download verification produces `verified`, with no cleanup candidate.
- Bad: the snapshot changes before OAuth, two new files appear, download identity differs, CPA refresh fails, or retry fails. The item becomes `failed` or `ambiguous`; every old filename remains untouched.

### 6. Tests Required

- Service tests assert snapshot persistence across process restart, normalized matching, unique replacement, same-name overwrite, missing/multiple replacements, duplicate filenames, retry failure, stale snapshot conflict, secret redaction, and review-only cleanup.
- API tests assert authentication, JSON/list limits, HTTP 409 conflict, HTTP 422 all-skipped behavior, sensitive response headers, worker reuse, and no remote deletion without an adapter.
- Template tests assert modern/legacy navigation parity, all four CPA-401 endpoints, snapshot/replacement columns, cleanup wording, and persistent audit wiring.
- Quality gates: `py_compile`, focused `unittest`, extracted inline scripts with `node --check`, and `git diff --check`.

### 7. Wrong vs Correct

#### Wrong

```python
if local_codex_status == "success":
    delete_cpa_files_for_email(email)
```

This treats local completion as remote proof, uses broad account identity, and risks deleting the only usable credential.

#### Correct

```python
service.mark_retrying([email], batch_id=batch_id)  # exact pre-retry filenames
run_existing_codex_retry(email)
result = service.detect({email: local_codex_status})
# Cleanup is available only after one exact replacement passes CPA status and download checks.
```

The default deployment has no delete adapter, so `cleanup()` returns an exact review queue and performs no mutation.
