# Protocol Registration Traffic Contract

## 1. Scope / Trigger

Use this contract when `REGISTRATION_DRIVER=protocol` creates a `BrowserSession`. One `ProtocolTrafficTracker` belongs to the complete protocol registration and is shared by the initial session, preflight/auth retries, session reopen, and direct fallback. Roxy, Cloak, Browser Use, Skyvern, Codex, liveness, password setup, email change, and ordinary `BrowserSession` callers remain uninstrumented unless they explicitly pass a tracker.

The observer covers constructor-time GeoIP GETs and raw curl_cffi GET/POST calls made by `BrowserSession`. It observes after a transport call returns or raises. It never retries, raises status, resets a circuit, reads a response body, consumes a stream, or changes request arguments. The tracker freezes before the separate 2FA stage.

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

### Persistence and freeze

The bounded aggregate is stored under `result["network_traffic"]`, registration-job `network_traffic`, and successful account `extra_json["network_traffic"]`; no migration is required. Failed protocol results include the aggregate whenever `run_registration()` can return a result. `BrowserSession.close()` does not freeze a shared tracker. Freezing is idempotent and occurs before 2FA, so later 2FA, Codex, flow-trigger, and post-registration requests cannot change totals.

Telemetry is observational. Initialization, extraction, freeze, or logging failures return an unavailable summary or omit one observation, but never replace a registration response or exception and never alter proxy, cookie, fingerprint, retry, circuit-breaker, email recovery, or account-persistence behavior.

### Account-list projection

The paged account-list API reads the persisted aggregate from account `extra_json["network_traffic"]` and exposes a separate compact projection. That projection requires `available=true` plus finite, non-negative numeric `total_bytes`, `upload_bytes`, `download_bytes`, `request_count`, `request_elapsed_seconds`, `http_error_count`, and `transport_error_count`. It may additionally expose finite, non-negative `unknown_size_request_count` and `incomplete_size_request_count`. Integer counters and byte fields must remain integers within the JavaScript safe-integer range; numeric strings may be coerced to numbers. Missing, unavailable, non-object, non-finite, negative, oversized, or otherwise malformed required values omit the whole projection, so old rows render `-`.

This is a fixed allowlist. Request details, URLs, arbitrary account extras, headers, bodies, credentials, tokens, exception text, and every other traffic field must not enter the compact account response. The V2 and Legacy account tables render the column immediately after the plan: total bytes on the main line, upload/download bytes on the subline, and request count, elapsed time, HTTP/transport failures, plus present unknown/incomplete counts in a numeric-only tooltip. Registration-job traffic rendering remains unchanged.

### Detail and confidentiality

`BROWSER_TRAFFIC_DETAIL_LOG` controls optional request rows for both browser traffic and protocol registration. Aggregate accounting remains active when this option is false. `BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES` is normalized to `1..10000` and enforced during collection; aggregate counters continue after the detail cap. Detail rows are emitted to the task log only and are not persisted in `network_traffic`.

Protocol detail URLs contain only a validated `http`/`https` scheme, host, and bounded sanitized path. Query strings, fragments, URL userinfo, proxy addresses, headers, cookies, request/response bodies, credentials, email addresses, OTPs, and exception text are never collected by this telemetry. Non-HTTP URLs are excluded.

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
- `tests/test_protocol_traffic_integration.py`: constructor/ordinary failure summaries, registration-job success/failure persistence, unavailable-summary fallback, and non-protocol driver isolation.
- `tests/test_config_defaults.py`: existing detail defaults and limits remain unchanged.
- `tests/test_webui_account_traffic.py`: compact account-list allowlist, numeric coercion/malformed persistence handling, absent/unavailable omission, V2/Legacy column placement, numeric-only rendering, tooltip contents, and unavailable-row fallback.
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
