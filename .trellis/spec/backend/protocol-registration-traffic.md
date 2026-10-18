# Protocol Registration Traffic Contract

## 1. Scope / Trigger

Use this contract when `REGISTRATION_DRIVER=protocol` creates a `BrowserSession`. One `ProtocolTrafficTracker` belongs to the complete protocol registration and is shared by the initial session, preflight/auth retries, session reopen, and direct fallback. Roxy, Cloak, Browser Use, Skyvern, Codex, liveness, password setup, email change, and ordinary `BrowserSession` callers remain uninstrumented unless they explicitly pass a tracker.

The observer covers constructor-time GeoIP GETs and raw curl_cffi GET/POST calls made by `BrowserSession`. It observes after a transport call returns or raises. It never retries, raises status, resets a circuit, reads a response body, consumes a stream, or changes request arguments. The tracker freezes before the separate 2FA stage.

### Post-authenticated bootstrap traffic policy

`CHATGPT_AUTH_BOOTSTRAP_ENABLED` defaults to `True` for protocol registration, preserving the existing authenticated ChatGPT warm-up after OAuth callback handling has produced `/api/auth/session` and an access token. Set `CHATGPT_AUTH_BOOTSTRAP_ENABLED=False` in `.env` to skip this optional traffic. The default does not change anonymous bootstrap, network preflight, Sentinel, OAuth/OTP/account-creation requests, callback/session persistence, or any existing retry/fallback behavior. Both protocol registration completion branches keep their conditional call.

## 2. Signatures

```python
BrowserSession(
    proxy: str | None = None,
    *,
    transport_impersonate: str | None = None,
    traffic_tracker: ProtocolTrafficTracker | None = None,
    ...,
)

ProtocolTrafficTracker.observe(
    *,
    method: str,
    url: object,
    response: object = None,
    error_kind: str | None = None,
    elapsed_seconds: object = None,
    streaming: bool = False,
) -> None
ProtocolTrafficTracker.snapshot() -> dict
ProtocolTrafficTracker.freeze() -> dict
ProtocolTrafficTracker.report(target_logger: logging.Logger | None = None) -> dict

run_registration(...) -> {
    "success": bool,
    "network_traffic": dict,
    ...,
}
```

## 3. Contracts

### Measurement

`request_count` represents one actual curl_cffi GET/POST invocation. A transport exception counts once as `transport_error`, even when it exposes a partial response. A returned HTTP status of 400 or greater counts once as `http_error`. The two failure categories are mutually exclusive.

`upload_bytes` uses native `request_size` only. In the verified curl_cffi build this already includes request-body bytes, so `upload_size` is retained as separate body evidence and is never added to or substituted for the complete upload total. `download_bytes` requires either native `response_size` or both `download_size` and `header_size`. Missing native values remain unknown instead of being treated as zero. Component-specific unknown counters preserve that distinction.

Redirects remain one logical invocation and increment `redirect_count`; whole-chain body totals are marked incomplete. Stream return-time snapshots are not drained and increment `incomplete_size_request_count`. These are application-visible curl metrics, not packet, TLS, proxy-tunnel, DNS, retransmission, or provider-billing bytes.

### Persistence and presentation

The bounded aggregate is stored under `result["network_traffic"]`, registration-job `network_traffic`, and successful account `extra_json["network_traffic"]`; no migration is required. Failed protocol results include the aggregate whenever `run_registration()` can return a result. Account metadata persistence is the durable registration summary and is intentionally not a presentation contract for the account list.

`BrowserSession.close()` does not freeze a shared tracker. Freezing is idempotent and occurs before 2FA, so later 2FA, Codex, flow-trigger, and post-registration requests cannot change totals.

Telemetry is observational. Initialization, extraction, freeze, or logging failures return an unavailable summary or omit one observation, but never replace a registration response or exception and never alter proxy, cookie, fingerprint, retry, circuit-breaker, email recovery, or account-persistence behavior.

The paged and legacy non-paged account-list API paths, plus both V2/Legacy account tables, omit `network_traffic` entirely. They do not render an account traffic column, tooltip, aggregate, URL, or any other traffic-derived value. Registration-job history and detail use the job `network_traffic` projection and continue to render its bounded aggregate in the registration task/job view. This separation keeps the durable account summary available for backend use without exposing registration traffic in account-list payloads or rows.

### Failure diagnostics

`register_user()` may inspect a bounded response-body prefix only on non-2xx `user/register` responses. It may log `status` plus a normalized code only when the code is in the fixed `_REGISTER_ERROR_CODE_ALLOWLIST`; arbitrary messages, unknown codes, response bodies, emails, passwords, tokens, cookies, and headers remain absent. The original response/exception behavior is preserved. This diagnostic is for classifying application-level 400/4xx failures and must not be used to weaken Sentinel validation or change retry behavior.


## 4. Validation & Error Matrix

| Condition | Required result |
| --- | --- |
| `BrowserSession` has no tracker | Preserve the exact existing call and response behavior |
| A tracked GET/POST returns 2xx/3xx | Count one completed request and native known byte/timing fields |
| A tracked GET/POST returns HTTP 4xx/5xx | Count one failed request and one `http_error`; preserve the response |
| Transport raises before a response | Count one failed request and one `transport_error`; re-raise the original exception |
| Transport exception includes a partial response | Keep available native evidence but classify only as `transport_error` |
| `request_size` is absent | Do not promote body-only `upload_size` to total upload; increment unknown total count |
| `response_size` and either body/header component are incomplete | Do not fabricate total download; increment unknown total/component counts |
| URL is non-HTTP | Exclude it from protocol request counts and details |
| Detail limit is reached | Stop retaining detail rows; continue aggregate counters and increment omitted count |
| Tracker observation/reporting raises | Preserve registration behavior and response/exception identity |
| Session is reopened or direct fallback starts | Reuse the same tracker; do not reset totals |
| Tracker is frozen before 2FA | Ignore later observations and return the same snapshot on repeated freeze |
| Protocol registration fails | Return and persist a bounded aggregate where the function can produce a result |

## 5. Good / Base / Bad Cases

- **Good:** a protocol registration uses proxy retries and direct fallback. Every real curl request is counted once across all sessions, the tracker freezes before 2FA, and the same aggregate reaches the result, job, and account metadata.
- **Base:** native metrics are partially unavailable or the request streams. Known component evidence is retained, unknown/incomplete counters are incremented, and no response body is consumed.
- **Bad:** body bytes are added to `request_size`, missing headers are silently treated as zero, session reopen resets totals, detail rows grow without bound, or a query/credential/body reaches logs or persistence.

## 6. Tests Required

- `tests/test_protocol_traffic.py`: exact native byte arithmetic; mutually exclusive HTTP/transport failures; partial/missing metrics; stream/redirect markers; URL sanitization; bounded details; freeze/report idempotence; public `BrowserSession.get()`/`post()` response and exception identity; constructor GeoIP observation; untracked-session isolation.
- `tests/test_protocol_preflight_session_rotation.py`: one shared tracker reaches initial, preflight retry, auth retry, reopen-error, authorize reopen, and direct-fallback factories.
- `tests/test_protocol_password_registration.py`: success aggregate persistence and freeze-before-2FA behavior.
- `tests/test_protocol_password_registration.py` and `tests/test_config_defaults.py`: authenticated bootstrap is enabled by default, can be explicitly disabled, and registration still finalizes OAuth/session state and returns/persists traffic totals.
- `tests/test_protocol_traffic_integration.py`: constructor/ordinary failure summaries, registration-job success/failure persistence, unavailable-summary fallback, and non-protocol driver isolation.
- `tests/test_config_defaults.py`: existing detail defaults and limits remain unchanged.
- `tests/test_webui_account_traffic.py`: account-list API omission, durable account metadata not projected, registration-job compact traffic availability, malformed/secret account data omission, restored V2/Legacy account layout, and retained registration-job rendering.
- Quality gates: focused tests, related WebUI and registration/session regressions, both templates extracted for `node --check`, full discovery compared with clean HEAD when required, `py_compile`, `compileall`, and `git diff --check`.

## 7. Wrong vs Correct

### Wrong

```python
# request_size already includes the body; this double-counts upload.
upload_bytes += response.request_size + response.upload_size

# Reopening a retry session silently loses prior observations.
session = BrowserSession(proxy=proxy, traffic_tracker=ProtocolTrafficTracker())
```

### Correct

```python
upload_bytes += response.request_size

# Every protocol session in one registration shares the same collector.
session = BrowserSession(proxy=proxy, traffic_tracker=registration_tracker)
```
