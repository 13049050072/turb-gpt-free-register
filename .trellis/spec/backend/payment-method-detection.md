# Payment Method Availability

## Entry point and eligibility

`core/chatgpt_plan.py:check_account_plan` runs best-effort payment detection after a successful accounts/check response. `check_payment_method_availability` requires a free plan, `plus_trial_eligible is True`, numeric discount 100, numeric duration 1, period `month`, and a valid matching promotion and checkout offer. Missing, malformed, generic, paid, and other-duration offers return `not_applicable` with no checkout request.

## Plan session initialization

- **Scope:** An internally owned `check_account_plan(...)` session warms up through `_warm_plan_session(env: BrowserSession) -> None` before building accounts/check headers. A borrowed `session=` is neither warmed nor closed by the plan checker.
- **Shared contract:** `_warm_plan_session` lazily calls `core.openai_auth.network_preflight(session: BrowserSession) -> None`. This reuses the registration flow's `/auth/login` top-level navigation, existing proxy-retry settings, preflight timeout, and successful document observation on the same tracked session. Document observation must precede accounts/check header generation so current page build metadata can be used.
- **Failure behavior:** Successful navigation observes the document. Recoverable navigation failures use the existing finite registration retry policy. An explicit HTTP 403 with `cf-mitigated: challenge` raises the fixed `InteractiveChallengeRequiredError("interactive challenge required")` after one request; both the shared request helper and outer authorize retry classifier treat it as terminal. Ordinary 403 responses retain same-session retries. Exhausted retries or other preflight errors remain best-effort: clear only the local plan circuit and proceed with accounts/check. The existing plan-loop retry/Retry-After rules and non-retryable 401 behavior remain unchanged.
- **Cases:** Good: login navigation succeeds and plan headers use the observed build. Base: login navigation fails but accounts/check still succeeds. Bad: a borrowed session is navigated or closed, or a separate transport is created during warmup.
- **Tests:** `tests/test_chatgpt_plan_retry.py` asserts navigation order, same-session 403 retry, document observation before plan headers, best-effort failure, borrowed-session boundaries, and owned-session cleanup.
- **Wrong vs correct:** Do not replace initialization with a bare homepage GET that never observes the returned document; call `network_preflight(env)` and preserve its shared request lifecycle.
- **Evidence limit:** A live accounts/check HTTP 200 after this change does not establish that initialization caused the recovery, especially when login navigation still returns 403. Report the independent checkout result separately; checkout HTTP 400 is not a successful payment-method check.

## Provider lifecycle

1. POST `https://chatgpt.com/backend-api/payments/checkout` using `_common_headers`, `plan_name: "chatgptplusplan"`, the eligible Plus promotion, and validated country/currency. `eligible_offer_ids` are opaque identifiers used for gating; they must never become the product name. Only the `plus` campaign may select the trial promotion.
2. Parse responses containing payment-method fields directly for backwards compatibility.
3. Otherwise validate `checkout_session_id` (or `id`) as a bounded `cs_` or `oaics_` identifier. Never follow a provider-supplied URL.
4. For `oaics_`, GET `/backend-api/payments/checkout/{processor_entity}/{session_id}` with ChatGPT authentication. Allow only `openai_llc` and `openai_ie`; default to the US entity for US billing and the IE entity otherwise.
5. For `cs_`, validate the `pk_live_` or `pk_test_` publishable key and POST form data to `https://api.stripe.com/v1/payment_pages/{session_id}/init`. Include the observed custom-checkout initialization parameters, browser locale/timezone, and Stripe API version. Only browser User-Agent/Accept-Language are copied; ChatGPT authorization, account/device headers, and cookies are not copied into request headers.
6. Parse the resulting snapshot with an explicit `oaics` or `stripe` source.

Use the same public `BrowserSession.get/post` wrappers for both requests. This preserves the proxy, transport, circuit breaker, and attached traffic tracker. Reject redirects and bound each request timeout to 1–60 seconds. No payment submission, confirmation, or separate retry/transport engine is added.

## Output and error contract

Persist only `payment_method_check_status`, `payment_method_checked_at`, `payment_method_error`, `payment_method_types`, `payment_method_labels`, `payment_session_type`, `payment_amount`, and `payment_currency`. Keep the DB second-boundary allowlist and existing API/V2/Legacy projections.

Errors use fixed local stage names (`checkout`, `oaics_state`, `stripe_init`) and bounded local diagnostics, such as `stripe_init: HTTP 429`. An HTTP 400 JSON object whose string `detail` is at most 512 characters and contains `unusual activity` (case-insensitive) maps to the fixed diagnostic `<stage>: provider_activity_restricted`; unknown or oversized details retain `<stage>: HTTP 400`. An explicit HTTP 403 with `cf-mitigated: challenge` maps to `<stage>: interactive_challenge_required`. Only fixed local labels may leave the parser; provider text is inspected in memory and never copied to the result. Never include exception text, provider error bodies, session IDs, publishable keys, checkout URLs, client secrets, tokens, or cookies. HTTP errors, malformed JSON, missing fields, and transport errors must not replace a successful plan result.

These labels identify observed provider rejection categories. They do not establish why the provider rejected the request, prove that changing product/region fields will fix it, or count as a successful payment-method check. Do not add challenge-solving, proxy rotation, or automatic request mutations in response to these categories.

`core/protocol_traffic.py:safe_protocol_url` redacts path segments beginning `cs_` or `oaics_`, even without digits. `core/session.py:_safe_url_for_log` reuses that sanitizer so both request accounting and circuit-breaker logs hide checkout identifiers.

## Verification

`tests/test_payment_method_detection.py` covers direct responses, two-step Stripe/OAICS fixtures, request order/headers, strict gating, invalid metadata, fixed failure diagnostics, persistence/API/template compatibility, public-wrapper traffic counts, and session-path redaction. Run related plan, traffic, session-header and UI regressions plus Python compilation and `git diff --check`.

Fixture success proves parser and request-dispatch behavior. A healthy deployment alone does not prove a provider request succeeded. Report live eligibility and sanitized provider stage/status separately; accounts without a current unambiguous one-month offer must remain skipped.
