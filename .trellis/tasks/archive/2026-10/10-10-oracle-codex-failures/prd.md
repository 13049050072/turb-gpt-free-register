# Diagnose Oracle Codex retry failures

## Goal

Explain why current online Codex retry operations fail using read-only production evidence and source inspection.

## Confirmed Scope

The user requested diagnosis of the existing Oracle deployment. Prior session authorization covers SSH access to `oracle`. Read-only container inspection, narrowly scoped SQLite queries, and sanitized log analysis are in scope. No new registration, OAuth login, OTP request, account modification, production configuration change, or deployment is authorized by this diagnosis.

## Known State

Observed at 2026-10-10 10:49:58 +08:00: container `turb-gpt-free-register` is running and healthy, with image `turb-gpt-free-register:codex-login-credentials-oracle-20261007T164500Z`, started 2026-10-07T08:59:02Z. Persistent mount is `/home/ubuntu/turb-gpt-free-register-compose/docker-data:/data`.

## Requirements

- Inspect recent account Codex status/error fields without copying credential payloads.
- Correlate failures with task logs and the deployed source code.
- Compare available historical successful records without triggering new work.
- Separate observed errors from hypotheses; explain affected stage and recovery options.
- Redact emails, phone numbers, passwords, OTPs, tokens, cookies, callback query strings, and proxy credentials before any output or evidence persistence.

## Acceptance Criteria

- [x] Online version and health are observed.
- [x] Recent failure distribution and representative sanitized error evidence are captured.
- [x] Failed transition is tied to an exact code branch.
- [x] Findings, confidence, limitations, and next actions are recorded.
- [x] Production remains unmodified by this investigation.

## Definition of Done

Evidence-backed diagnostic report plus a rendered HTML explanation. No business code change, commit, or deployment required.

## Out of Scope

Actual retry execution, account creation, risk-control bypass, network/identity rotation, credential export, production fixes.
