# Account Inspection Acceptance

## Scope

This change covers the account-inspection failure-details boundary and the modern/Legacy account-list refresh state machine.

- Failure text is bounded and redacted before persistence and again before API serialization. The redaction covers credentials, bearer/basic authorization values, OAuth codes and callback URLs, proxy credentials, header-like fields, and token-shaped values.
- `inspection_id` is validated as a positive SQLite signed 64-bit integer before the failure-details query. Out-of-range values return a bounded `400` and do not reach SQLite.
- The failure-details endpoint applies `Cache-Control: no-store, max-age=0`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff` to successful, validation-error, and not-found responses.
- `loadAccounts()` keeps one request in flight, coalesces overlapping refreshes, skips an explicitly invalidated old snapshot, and still renders each latest completed response during continuous polling.

## Reproducible Verification

Run from the repository root:

```sh
.venv/bin/python -m unittest tests.test_account_inspection
node tests/test_account_list_reload_ui.cjs
node tests/test_account_inspection_ui.cjs
.venv/bin/python -m py_compile core/error_redaction.py core/db.py core/account_inspection_service.py core/credential_health_scheduler.py webui/app.py
git diff --check
```

Observed pre-merge results for this change:

- `tests.test_account_inspection`: **82 tests, OK**.
- `tests/test_account_list_reload_ui.cjs`: **passed** for `index.html` and `index_legacy.html`, including continuous overlap, invalidated refreshes, page correction, failure, and timeout recovery.
- `tests/test_account_inspection_ui.cjs`: **passed**.
- `py_compile`: **passed** for all changed Python modules.
- `git diff --check`: **passed**.

The full repository suite is not claimed here. Two known clean-HEAD failures remain outside this pre-merge fix set; they were not reclassified as regressions. No Oracle access, deployment, or real account-network verification was performed.

## Residual Risks

The redaction is pattern-based and intentionally bounded; future secret field names or opaque binary formats need an explicit pattern addition. The UI remains process-local browser state and assumes the existing single-WebUI-worker deployment model. Focused tests use mocks and fake secrets, not real login, OAuth, proxy, or account-service traffic.
