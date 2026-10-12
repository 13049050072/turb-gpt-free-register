# Oracle Codex retry failure diagnosis

## Snapshot and scope

- Requested objective: explain current online Codex retry failures.
- Observation date: 2026-10-10; SQLite snapshot at container-local 10:52:39. Account/log timestamps use the same local clock. Latest batch events span 10:42:55–10:44:35.
- Connection: existing SSH host alias `oracle`.
- Running object: Docker container `turb-gpt-free-register`.
- Container image: `turb-gpt-free-register:codex-login-credentials-oracle-20261007T164500Z`.
- Container state observed: running, healthy; started 2026-10-07T08:59:02Z.
- Persistent data: `/home/ubuntu/turb-gpt-free-register-compose/docker-data`, mounted `/data`.
- Read-only SQLite connection: `sqlite3.connect('file:/data/turb.sqlite3?mode=ro', uri=True)` with `PRAGMA query_only=ON`.
- Only status/error/timestamp/ID fields were emitted. Email used internally to locate log files and redacted before output. No credential payloads were copied.
- No production configuration changes, container replacement, registration, OAuth attempts, OTP requests, new phone acquisition, or balance API calls were performed.

## Latest batch outcome

Account IDs 830–849 (20 account records updated today with non-empty/non-skipped Codex state):

| Recorded final state | Count | IDs | Evidence |
|---|---:|---|---|
| failed: balance error | 5 | 842, 843, 844, 845, 846 | `SmsNoBalanceError: 接码平台余额不足（NO_BALANCE），请充值` |
| failed: phone verification retry exhausted | 3 | 847, 848, 849 | `RuntimeError: [Codex] 手机号验证重试 10 次仍失败（provider=grizzly）` |
| stopped | 12 | 830–841 | `用户手动停止 Codex 补跑` |
| success | 0 | — | No success among today's matching records |

The full database also contains 717 skipped, 19 success, 12 stopped, and 9 failed records. One failed record (ID 637) predates this batch: updated 2026-10-06T14:29:58. Do not count that record as today's failure. Latest stored successful record is ID 633, updated 2026-10-06T14:27:50. These are stored timestamps, not a controlled comparison of deployed versions.

## Phone verification evidence

The observed phone-stage failure messages name `provider=grizzly`. This is the compatibility adapter label, not sufficient evidence of vendor identity. Follow-up verification of the persisted and reloaded configuration confirmed `SMS_API_BASE=https://hero-sms.com/stubs/handler_api.php` and `SMS_COUNTRY=73` (Brazil), so the actual configured platform is HeroSMS. The earlier user-facing GrizzlySMS vendor attribution was incorrect; see the follow-up correction below. No live balance API was called.

Failed account logs contain 31 `add-phone/send` HTTP 400 rejections:

| Error code | Count of rejected requests | Response text |
|---|---:|---|
| `phone_number_in_use` | 20 | `Phone number already in use. Please use a different phone number.` |
| `fraud_guard` | 11 | `We've detected suspicious behavior from phone numbers similar to yours. Please try again later or contact us through our help center at help.openai.com.` |

Per-account rejected-request counts:

- ID 846: one `fraud_guard` rejection before final `NO_BALANCE` failure.
- ID 847: 10 rejected requests; 7 `phone_number_in_use`, 3 `fraud_guard`.
- ID 848: 10 rejected requests; 6 `phone_number_in_use`, 4 `fraud_guard`.
- ID 849: 10 rejected requests; 7 `phone_number_in_use`, 3 `fraud_guard`.

Representative sanitized lines:

```text
10:43:03 [WARNING] [Codex] add-phone/send 未成功 reason=send_rejected, status=400: Phone number already in use. Please use a different phone number. invalid_request_error phone_number_in_use，换号重试
10:42:57 [WARNING] [Codex] add-phone/send 未成功 reason=send_rejected, status=400: We've detected suspicious behavior from phone numbers similar to yours. Please try again later or contact us through our help center at help.openai.com. invalid_request_error fraud_guard，换号重试
10:43:57 [WARNING] [Codex] 失败：<EMAIL_REDACTED>，RuntimeError: [Codex] 手机号验证重试 10 次仍失败（provider=grizzly）
10:44:18 [WARNING] [Codex] 失败：<EMAIL_REDACTED>，SmsNoBalanceError: 接码平台余额不足（NO_BALANCE），请充值
10:44:24 [WARNING] [Codex 补跑] <EMAIL_REDACTED> 已停止: 用户手动停止 Codex 补跑
```

These logs directly identify rejection at the phone-send stage. They do not support blaming password login, email OTP, callback collection, or token persistence. They also do not prove which additional hidden factors contributed to server-side `fraud_guard`.

## Source mapping

- `core/sms_provider.py:87`: `if text == "NO_BALANCE":` raises `SmsNoBalanceError`.
- `core/codex_oauth.py:1248`: `_do_phone_verification` uses `SMS_MAX_RETRIES`.
- `core/codex_oauth.py:1262`: sends the phone verification request.
- `core/codex_oauth.py:1270`: non-200/204 response or detected failure reason enters the warning/cancel/retry branch.
- `core/codex_oauth.py:1320`: `except sms_provider.SmsNoBalanceError:` immediately propagates the balance error.
- `core/codex_oauth.py:1331`: exhausted attempts become a generic `RuntimeError` with provider and retry count.
- `core/codex_oauth.py:838`: `_phone_failure_reason` groups these HTTP 400 bodies as `send_rejected`; the exact provider response remains visible in the logs but is lost from the final generic retry-exhausted error. `last_err` is only populated by `SmsProviderError` exceptions.
- `core/codex_retry_service.py:233`: records result status and message in the account record.
- `core/codex_retry_service.py:132` and `core/codex_retry_service.py:238`: stop request/exception handling records `stopped`; this state must be distinguished from a completed failed verification.

Observed local and running-container SHA-256 hashes match:

```text
core/codex_oauth.py b6cd68aff47c010092fcb08360800ef995fb49645001479b7654c88cacc6bee2
core/sms_provider.py 3363a41cf3d090f1c040284b30a92e5440249c6ef4729fdfed2da6079dd9ed45
core/codex_retry_service.py 4b80a01e4532d8471b414de70f4ca0a9a5bc1a3c745312eb0327c5547f53c71a
webui/app.py 216bb71832c5171fcad7f357bffd901bfabac1a85d53d96a2e8a2d35b1502855
```

`git diff --name-only 2fd542a..9988683 -- core/codex_oauth.py core/sms_provider.py core/codex_retry_service.py` emitted no paths (exit 0): the preceding login-credentials-export deployment did not change these three authentication/SMS/retry source files. This narrows source-regression suspicion; it is not proof that every deployment/environment variable is identical.

## Conclusion and limits

The latest batch has zero recorded successes: five final balance failures, three final retry-exhaustion failures caused by observed phone-send rejections, and twelve manual stops. The observed blockers are phone number already in use, server-side phone-related fraud guard, and the SMS provider's reported insufficient balance.

A healthy container alone does not validate every application path. The evidence locates the latest failures in the phone-verification flow; it does not establish a deployment regression. Resolving balance would address `NO_BALANCE` only; it would not establish that the occupied-number or risk-control errors are resolved. The logs do not justify changing proxy or other identity settings to evade rejection. Follow the verification service's normal account/phone verification and retry guidance for affected accounts.

Suggested observability follow-up, not implemented: retain the last structured phone rejection code in the final account error so the UI can distinguish occupied-number, fraud guard, and generic transport failures. User asked for diagnosis, so no business code patch or deployment was performed.

## Tool-error ledger

- Local `shasum`/`rg` assumed `tasks/runner.py`, which does not exist. The command exited 2; no production mutation occurred. Actual retry worker was located at `core/codex_retry_service.py`.
- Follow-up `rg` still included absent `tasks/`, yielding `rg: tasks: No such file or directory (os error 2)` in output. Valid source hashes were subsequently observed for the three existing files.
- Another local lookup included absent `core/hot_config.py` and `core/config_hot.py`, exited 2. Actual reload entry point observed in `core/codex_retry_service.py:202` is `config_pkg.reload_all()`. These failed local lookups do not constitute production failures.
- A read-only review subagent timed out before producing a completed review. No independent-review claim is made. The main agent verified cited source branches directly and corrected source line references.

## Verification and artifacts

- Main-agent source review verified the phone-send rejection condition, immediate balance exception propagation, final generic error construction, status persistence, and manual-stop state.
- The 20 final states reconcile as `5 + 3 + 12`. The 31 rejected requests reconcile as `1 + 10 + 10 + 10` and `20 phone_number_in_use + 11 fraud_guard`.
- `git diff --check` exited 0. At the end of the diagnosis, tracked business files were unchanged; only task documentation was untracked.
- HTML render completed with four panels and STE zero warnings; reopened the generated file and this report.
- Rendered page: `/Users/jiangdk/.answer-me-with-html/pages/Oracle-Codex-补跑失败诊断-20261010-105615.html`. The platform attribution in that original page is superseded by the follow-up below.

## Follow-up correction: platform and country (2026-10-10)

User asked which country is used and whether the configured platform is HeroSMS. At container-local 11:01:45, a read-only process loaded configuration using the same `config.reload_all()` entry point called by the retry worker. Persisted `.env` values and resulting configuration agreed:

```text
SMS_PROVIDER=grizzly
SMS_API_BASE=https://hero-sms.com/stubs/handler_api.php
SMS_COUNTRY=73
SMS_SERVICE=dr
SMS_MAX_PRICE=0.06
SMS_MAX_RETRIES=10
```

`/app/.env` resolves to `/data/.env`; its observed modification timestamp was 2026-10-09T11:53:31, preceding today's failed batch. Secret values were neither printed nor copied. The fresh-process config probe did not reload or mutate the existing WebUI process.

The public, unauthenticated HeroSMS country metadata endpoint `https://hero-sms.com/stubs/handler_api.php?action=getCountries` returned the following selected record:

```json
{"id":73,"rus":"Бразилия","eng":"Brazil","chn":"巴西","visible":1,"retry":1,"rent":1}
```

Therefore the configured number-acquisition request is **HeroSMS / Brazil (country ID 73)**. Country IDs are provider country identifiers, not telephone dialing prefixes. This confirms the requested country, not a separately observed returned telephone number.

`core/sms_provider.py:76` builds request authentication internally; `core/sms_provider.py:78` sends the HTTP request to `_cfg.SMS_API_BASE`. The `_request_grizzly` function name and `SMS_PROVIDER=grizzly` refer to the handler-API compatibility adapter. They do not override the configured HeroSMS host. The earlier final response mistakenly interpreted this adapter label as the actual vendor.

The prior batch's `NO_BALANCE` errors must thus be attributed to the configured **HeroSMS interface**. The observed `phone_number_in_use` and `fraud_guard` errors still originate at the account verification service; failure counts and stages are unchanged. The investigation did not query current account balance, acquire numbers, modify country/platform configuration, or trigger retries.