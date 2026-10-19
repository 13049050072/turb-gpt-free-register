# Payment method availability detection

## Goal

Add sanitized payment-method availability detection to the existing account plan-check flow. Reuse the current project session, proxy, retry, traffic accounting, database payload, API projection, and V2/Legacy account UI. Do not copy the comparison project's wreq backend, fingerprint generator, Sentinel implementation, proxy rotation, or protocol registration engine.

## Semantics

The feature reports payment methods returned by the current checkout session for an account and region. It does not claim to read a saved card or prove that a specific payment instrument is attached to the account. Checkout initialization is permitted only when the existing plan result unambiguously identifies a one-month free trial: a free plan with `plus_trial_eligible=true`, a 100% discount, numeric duration `1`, and period `month`, plus well-formed promotion and offer identifiers. Generic promotions, paid plans, zero or other durations, unknown/malformed duration fields, and malformed offer data are non-applicable and must not create checkout traffic.

## Scope

- Run after the existing plan/promo lookup only for an unambiguous one-month free-trial result; generic eligible promotions, paid plans, other trial durations, missing/unknown offer fields, and malformed offer data must not trigger checkout/payment detection.
- Reuse the authenticated plan-check session and configured proxy route where the current flow permits it.
- Support the OAICS checkout response and Stripe payment-page initialization response shapes observed in the comparison project.
- Normalize method identifiers and expose human-readable labels from a single backend mapping.
- Persist only bounded non-secret fields in the existing account payload/plan result JSON:
  `payment_method_check_status`, `payment_method_checked_at`, `payment_method_error`, `payment_method_types`, `payment_method_labels`, `payment_session_type`, `payment_amount`, `payment_currency`.
- Add the fields to the existing plan API projection and both V2/Legacy account views.
- Preserve plan-check behavior when the payment endpoint is unavailable, malformed, rate-limited, or returns an unknown method. Payment detection is best-effort and must not turn a successful plan check into a failed plan check.
- Do not store checkout URLs, session IDs, client secrets, card data, cookies, authorization values, request/response bodies, or proxy credentials.
- Keep existing protocol traffic accounting in effect for any requests made through a tracked session.

## Non-goals

- Charging, confirming, or submitting a payment.
- Reading saved cards or payment instruments from a billing portal.
- Adding a new dependency.
- Copying vendor-specific transport/fingerprint/Sentinel code.
- Automatically running the new check for every account outside the existing plan-check operation.

## Acceptance criteria

- [ ] A free account with an eligible promotion can produce sanitized method types, labels, session type, amount, and currency.
- [ ] OAICS and Stripe response fixtures are parsed deterministically.
- [ ] Unknown methods remain available as bounded identifiers with a generic label; parser errors do not crash plan checks.
- [ ] Only an unambiguous one-month free-trial result triggers checkout; generic promotions, paid plans, zero/other/unknown durations, and malformed offers skip without checkout traffic.
- [ ] HTTP, transport, malformed-response, and missing-field failures are persisted as bounded best-effort status and never replace a successful plan result.
- [ ] No secrets, checkout URLs, IDs, tokens, cookies, or response bodies reach persistence, API output, logs, or UI.
- [ ] Existing plan fields and account-list payloads remain backward compatible.
- [ ] Both V2 and Legacy UIs display the payment-method result with a clear not-checked/error state.
- [ ] Focused tests, related plan/API/UI regressions, `py_compile`, `compileall`, and `git diff --check` pass.

## Design decision

Use a small local parser/result model in the existing plan-check layer. Adapt only the response-shape knowledge from `wangshen233/turb-gpt-free-register-oss-20260928`; do not import or vendor its transport stack. Keep the detection opt-in through the existing plan-check operation and default it to the least-surprising behavior for current operators unless the existing configuration already has a payment-check switch.
