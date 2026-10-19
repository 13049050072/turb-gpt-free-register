# Checkout 400 and login-page 403: local source investigation

Scope: HEAD `a731abd`; local source and existing local records only. No network requests, source edits, deployment, or commit. This note contains no credential, header, cookie, proxy, request-body, or response-body values.

## Subsequent main-agent live evidence

The original investigation below was local-only. A later read-only trace on Oracle image `a731abd`, account 918, is recorded under `CHECKOUT DIAGNOSTIC a731abd` in `.tmp/payment-flow-transaction/VERIFICATION.txt`:

- Login navigation returned 403 on three requests; each response had `cf-mitigated: challenge`, HTML content, and a recognized challenge-page marker. This is an explicit interactive-challenge response, not an established JSON/schema failure.
- Accounts/check returned 200; the strict one-month-trial gate and matching-offer check both succeeded.
- Checkout returned 400 with JSON content, a top-level `detail` string, no challenge header, and locally fixed diagnostic word flags `unusual` and `activity`. This indicates a provider activity restriction; the exact policy trigger is not observable from this evidence. No raw error string was retained.
- The diagnostic verified JSON dispatch, standard Plus product, custom checkout mode, a nonempty campaign, and billing/session-region consistency as booleans. It did not prove every upstream rule is satisfied.
- Checkout failed before OAICS/Stripe initialization. No account database result was written.

These observations supersede the earlier evidence gap about response categories. They do not prove that login 403 caused checkout 400, or that copying registration's bot-control machinery would resolve either response. Explicit challenges should stop automatic challenge retries, and payment errors should expose a bounded local classification without provider content.

## Evidence boundary (original local-only investigation)

- `.tmp/payment-flow-transaction/VERIFICATION.txt:275` records a successful plan response (200), an exact one-month trial gate, and a separate `checkout: HTTP 400` failure. The associated summary records three upstream login-preflight 403 responses (`.tmp/payment-flow-transaction/summary.md:44`). These are earlier recorded observations, not a new live check.
- The older `.tmp/answers/payment-method-http400-deployed.html:894` says the 400 was fixed and attributes it to the request contract. The newer recorded checkout 400 does not support that conclusion.
- At the time of the original local-only investigation, the source and records did not identify either provider response category. See the subsequent main-agent evidence above; raw provider error bodies are still not retained here.

## Current plan → checkout path

1. `core/chatgpt_plan.py:1093` constructs a session only when one is not supplied. Owned sessions call `_warm_plan_session` before accounts/check headers are built (`:1114`, `:1122`). Borrowed sessions are neither warmed nor closed (`:1222`).
2. `_warm_plan_session` calls `network_preflight`; it catches failure and clears the local circuit (`:1006`). Login preflight follows redirects, checks HTTP status, and observes the document only after success (`core/openai_auth.py:295`). Document observation requires a ChatGPT final hostname (`core/session.py:609`). A rejected login page therefore cannot refresh page metadata through this path.
3. Successful accounts/check parsing precedes the strict trial gate and checkout (`core/chatgpt_plan.py:1155`, `:1164`, `:421`). Login preflight failure does not prevent a later plan success. Clearing a client circuit does not resolve an upstream rejection.
4. Checkout uses the fixed Plus product, campaign selection, billing region, custom mode, and `json=` dispatch (`:602`, `:614`). It disables redirects and bounds each request timeout (`:600`, `:620`). Metadata then selects an allowlisted OAICS state request or Stripe form initialization (`:627`–`:655`). A checkout-stage 400 ends the operation before either follow-up.
5. `_payment_response_json` rejects non-2xx status before reading JSON (`:528`). Thus the bounded payment result reports stage/status but intentionally discards error detail. Payment failure leaves the successful plan result intact (`:1163`).

Encoding check: `_common_headers` removes the default content-type (`:868`), but checkout passes `json=`. The installed local curl_cffi source serializes that argument as JSON and sets the JSON media type (`.venv/lib/python3.14/site-packages/curl_cffi/requests/utils.py:664`, `:747`). Stripe passes form data and builds its own media-type header (`core/chatgpt_plan.py:542`). There is no demonstrated missing-JSON-media-type defect here. The deployed dependency version was not inspected in this turn.

Transport check: public `BrowserSession.get/post` forward kwargs, attach target metadata only for the appropriate host, account for traffic, and observe the circuit (`core/session.py:776`, `:835`). No payment redirect-following bug was found. Explicit Stripe headers are selected from a narrow browser-field allowlist, rather than copied wholesale (`core/chatgpt_plan.py:546`).

## Confirmed local edge cases, not deployed-400 causes

| Finding | Source | Network-disabled probe result | Suggested regression |
| --- | --- | --- | --- |
| With no campaign-to-offer link, multiple offers select the first offer. This conflicts with the specification's unambiguous matching-offer requirement. | `core/chatgpt_plan.py:474`–`:485` | Two unrelated fixture offers are accepted. | Ambiguous offers should skip checkout; exercise explicit matches and order permutations. The existing test at `tests/test_payment_method_detection.py:287` currently accepts the permissive fallback. |
| A language-only locale can become a country when geography is absent and a fallback billing currency exists. Validation checks letters, not country semantics. | `core/chatgpt_plan.py:508`–`:521` | A language subtag is accepted as a checkout country. | Reject language-only and unknown country codes; cover language-region and script-region locales. Keep region unavailable rather than inventing a region. |
| Promotion metadata is dereferenced without a mapping-type guard. | `core/chatgpt_plan.py:909`–`:913` | A truthy list in metadata raises `AttributeError`. | Malformed campaign, metadata, discount, and duration types should preserve usable plan data and skip payment detection. This failure precedes checkout and cannot explain an already observed checkout 400. |

The probes executed the current plan module's AST in memory, excluding its project-session import, using synthetic sessions and responses. Socket connect operations were disabled; bytecode writes were disabled. They checked actual source functions without running registration or constructing real sessions. These are targeted probes, not a full regression-suite run.

A fourth probe returned exactly `checkout: HTTP 400`, with one request, JSON dispatch enabled, redirects disabled, and zero calls to the response JSON reader. This demonstrates the diagnostic gap, not the provider's rejection reason.

## Exact protocol-registration initialization order

This applies to the protocol branch of `run_registration`; browser drivers dispatch elsewhere (`main.py:564`).

1. Resolve/acquire email first; create the traffic tracker before constructing the session (`main.py:614`, `:627`, `:632`).
2. Session construction installs the tracker, resolves transport configuration, allocates session state, creates the transport and timeout/circuit state, optionally resolves geography, selects the browser profile, and seeds first-party cookie state (`core/session.py:119`–`:253`). No values are reproduced here.
3. Run login-document preflight. Within the session, its finite retry helper checks status before document observation (`core/openai_auth.py:269`, `:295`).
4. If anonymous bootstrap is enabled, run anonymous accounts/check → CES settings → anonymous me → country pricing configuration when a country is available (`main.py:166`; `core/chatgpt_bootstrap.py:139`). Bootstrap is best-effort by default (`config/openai_protocol.py:48`, `:53`).
5. Run providers discovery → CSRF acquisition → signin (`main.py:175`, `:177`, `:179`). Providers discovery specifically tolerates 403/429; CSRF and signin check status (`core/chatgpt_auth.py:79`, `:136`, `:173`). The protocol registration path does not call the separate anonymous-session probe used by liveness.
6. Follow authorization redirects, then ensure the password page (`main.py:233`, `:687`). Existing outer recovery can reconstruct the entire pre-OTP chain; this describes current behavior, not a proposed 403 remedy (`main.py:206`, `:274`).
7. Generate the password, invoke the existing bot-control integration, submit user/register, then navigate to OTP send (`main.py:690`–`:710`). After OTP validation, follow the server-selected callback or profile-creation branch, then complete callback/session retrieval (`:744`, `:790`, `:823`, `:848`).

Normal session setup includes document observation, one consistent cookie jar/transport, CSRF form encoding, signin, and legitimate redirect handling. Bot-control integration and identity/route rotation are distinct mechanisms. Their presence in existing registration or comparison code is not evidence that checkout needs them. No solver, fingerprint, proxy-rotation, or bypass changes are recommended.

## Existing comparison source

`.tmp/payment-flow-reference/promo_check.py:219` uses the same checkout schema and JSON dispatch; its Stripe form parameters at `:305` correspond to the current helper. It additionally fetches authenticated country pricing configuration and warms a document before checkout (`:199`, `:211`). The current owned plan flow warms only the login document and then queries accounts/check. These are source-backed context differences, not proof that the extra pricing request is mandatory or that its absence caused 400. The reference also contains bot-control and alternate-transport machinery; those are outside the proposed diagnostic work.

## Safe failure diagnostics to gather next

Keep public payment errors bounded. Add only a reviewed local diagnostic record containing:

- Fixed stage, integer HTTP status, attempt number, and elapsed duration.
- Local categories for response format and JSON parse outcome; whether an error object exists.
- Error classification only through an explicit reviewed allowlist, with an unknown fallback. Never echo arbitrary error codes, messages, keys, values, field paths, or body previews. Recognized field names must also be allowlisted.
- Local request checks as booleans: schema valid, campaign-offer relationship proven, region recognized, serializer mode correct, authentication context present and consistent. Record no underlying values.
- Warmup outcome separately: status, redirect count, final-origin category, document-metadata observation booleans, and whether a request was suppressed by the local circuit.

Diagnostic collection must not use existing body-preview or exception-text paths as a template: plan failures retain `response_preview` (`core/chatgpt_plan.py:1135`, `:1151`), warmup/retry logging includes exception text (`:1013`; `core/openai_auth.py:289`), and bootstrap diagnostics can include arbitrary response values (`core/chatgpt_bootstrap.py:28`, `:82`). Truncation alone does not redact secrets.

## Focused offline test suggestions

1. Exercise the three edge cases above through the full plan-to-checkout boundary. Assert ambiguous or malformed eligibility does not make a checkout request.
2. Add a fake-transport serialization assertion below `BrowserSession`, without any network. Verify JSON decoding/schema for checkout and form decoding/schema for Stripe; report assertion outcomes only. The current contract fake at `tests/test_payment_method_detection.py:71` validates kwargs and invents a 400 on mismatch; it cannot prove real provider acceptance.
3. Add the exact stage sequence: exhausted login 403 → plan 200 → checkout 400. Assert successful plan retention, no provider follow-up, separate stage diagnostics, borrowed-session ownership, and observed-document ordering. Existing warmup tests cover related lifecycle behavior (`tests/test_chatgpt_plan_retry.py:110`, `:175`).
4. Test diagnostics against JSON, HTML, malformed JSON, absent status, redirects, unknown error codes, and secret-like strings in every candidate field and exception. Assert only fixed enums, bounded numbers, and booleans leave the collector. Existing provider-error tests are a useful starting point (`tests/test_payment_method_detection.py:419`).

Validation: the eight investigated source/test files matched HEAD byte-for-byte. No tracked changes or commits were made. The only task artifacts created by this investigation are this research note and a local HTML summary.
