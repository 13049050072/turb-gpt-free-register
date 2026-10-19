# Payment-method detection implementation notes

## Contract

Payment detection is part of the existing successful plan-check operation. It runs once only after `accounts/check` has returned a result that clearly identifies a one-month free trial: free plan, `plus_trial_eligible=true`, 100% discount, numeric `plus_trial_duration_num_periods=1`, and `plus_trial_duration_period=month`, together with a well-formed promotion and matching checkout offer. Generic promotions, paid plans, zero or other durations, missing/unknown duration fields, and malformed offer data return `payment_method_check_status=not_applicable` without a checkout request. It reuses the current authenticated `BrowserSession`; no separate proxy, retry loop, transport stack, or payment client is created.

The parser accepts provider camelCase and nested fields used by the observed fixtures, including OAICS `paymentMethodTypes`/`customPaymentMethods`, `total_summary.due`, and `invoice.amount_due`, plus Stripe `payment_method_types`, `amount_total`, and invoice totals. It never follows a hosted payment URL or stores provider response data.

The only persisted and projected payment fields are:

- `payment_method_check_status` (`checked`, `not_applicable`, or `error`)
- `payment_method_checked_at`
- `payment_method_error`
- `payment_method_types`
- `payment_method_labels`
- `payment_session_type`
- `payment_amount`
- `payment_currency`

The DB layer applies a second allowlist and bounds check. HTTP, transport, malformed, missing-field, and unknown-method outcomes are best effort: they remain payment status data and never change an otherwise successful plan result. Checkout URLs, session IDs, client secrets, card data, cookies, authorization values/tokens, request/response bodies, and proxy credentials are not copied into the result, logs, API projection, or UI.

## Conservative choices

- The existing plan-check queue and account payload format remain unchanged; no migration or new configuration switch is required.
- The checkout initialization path is `POST /backend-api/payments/checkout`. Its product name is the constant `chatgptplusplan`; opaque `eligible_offer_ids` are used only for eligibility checks. The promotion ID must come from the eligible `plus` campaign. The response itself is never persisted.

Checkout carries browser risk headers: `openai-sentinel-token` and, when the SDK emits it, `openai-sentinel-so-token`, minted through the existing Sentinel runner with flow `chatgpt_checkout` and page URL `https://chatgpt.com/?promo_campaign=<promotion id>`. The same promotion ID is used as the request `Referer`. Token minting is best effort: a failure still sends the checkout request without the headers, records no token text, and clears only the local circuit breaker so the checkout request is not blocked by a challenge on the token endpoint.

Provider activity restrictions, interactive challenges, and 5xx responses to the checkout request are retried once with a freshly minted token; every other outcome is final. Both attempts stay inside the existing per-account plan-check operation and reuse the same session.
- A free plan with a promotion but no checkout offer, or with an ambiguous/malformed offer, is treated as not applicable, avoiding speculative payment traffic.
- The exact one-month gate is evaluated from the existing plan result fields; no new plan field or configuration switch is introduced.
- Payment parsing never follows provider-supplied URLs. A direct method snapshot is parsed immediately; otherwise a validated `oaics_` session uses the authenticated checkout-state GET, and a validated `cs_` session plus publishable key uses Stripe's form-encoded payment-page init. Both use the existing public session wrappers, proxy and traffic tracker. Stripe headers exclude ChatGPT authentication/account/device/cookie fields.
- Errors contain only fixed local stages (`checkout`, `oaics_state`, `stripe_init`) and bounded diagnostics. Both traffic and circuit-breaker URL logging redact `cs_`/`oaics_` path segments, including alphabetic fixtures.

## Verification

Focused parser, gating, failure, redaction, persistence/API projection, and both-template tests are in `tests/test_payment_method_detection.py`. The implementation should be validated with those tests plus the related plan, account API/UI, Node template syntax, `py_compile`, `compileall`, and `git diff --check` commands recorded in the completion report.
