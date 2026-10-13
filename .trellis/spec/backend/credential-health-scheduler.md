# Credential Health Scheduler

> Executable contract for periodic Sub2API401 and ChatGPT2API401 inspection.

## Scope

The scheduler runs inside the Flask/Gunicorn process and supports exactly two independent jobs: `sub2api_401` and `chatgpt2api_401`. It is valid only while production uses one Gunicorn worker process. Browser pages configure and observe jobs but never trigger periodic mutation endpoints.

## Public Signatures

```python
CredentialHealthScheduler.state(job_name: str) -> dict
CredentialHealthScheduler.configure(job_name: str, *, enabled: bool, interval_minutes: int) -> dict
CredentialHealthScheduler.run_due(now: datetime | None = None) -> list[str]
CredentialHealthScheduler.update_progress(job_name: str, run_id: str, *, phase: str | None = None, **counts: int) -> bool
CredentialHealthScheduler.start() -> None
CredentialHealthScheduler.stop() -> None
```

```text
GET /api/sub2api-401/scheduler
PUT /api/sub2api-401/scheduler       {"enabled": boolean, "interval_minutes": integer}
GET /api/chatgpt2api-401/scheduler
PUT /api/chatgpt2api-401/scheduler   {"enabled": boolean, "interval_minutes": integer}
```

Each existing product `state` response also includes its sanitized `scheduler` projection.

## Configuration and Cadence

- Default: disabled, 30 minutes.
- Valid interval: literal integer `5..1440`; booleans, floats, strings, and out-of-range values are rejected.
- Enabling a disabled job persists `next_run_at=now` and wakes the coordinator for one immediate run.
- Disabling does not abort an already-running exact-ID transaction; it prevents the next run.
- Changing an enabled idle interval schedules `now + interval`; changing a running interval applies after completion.
- Cadence is completion-relative: `next_run_at = last_finished_at + interval`.
- A job is single-flight; overdue time does not create catch-up runs.
- Scheduler construction is process-singleton by resolved state path. Repeated Flask app factories in one process reuse one state snapshot, lock, executor, coordinator, and active-future map instead of clobbering settings or duplicating a job.
- The two products use separate executor slots, so one long run does not block the other.

## Persistence and Recovery

`credential_health_scheduler.json` is versioned and contains only config, timestamps, phase, run ID, bounded counts, outcome, and redacted bounded error. Writes use create-exclusive temporary files, flush/`fsync`, `os.replace`, and mode `0600`; symlink targets remain on the data volume. Docker maps this filename to `/data`. `CREDENTIAL_HEALTH_SCHEDULER_PATH` may override the state path for tests or an explicit deployment volume.

Persisted `running` or `waiting_for_queue` state is reconciled to `last_outcome=interrupted`. If still enabled, one replacement run is immediately due. Product services remain authoritative for account-level recovery and evidence.

## Workflow Adapters

### Sub2API401

1. Refuse an automatic round while a matching Codex retry thread is actually alive. After process restart, persisted `retrying` rows whose local status is also stale are reconciled as failed evidence before detection so they cannot block forever.
2. Call the existing `Sub2Api401Service.detect()` once.
3. Select only fresh rows where `state=pending`, `health=unauthorized`, and `retryable=true`, capped by the existing 50-ID limit.
4. Call the same shared retry coordinator used by `POST /api/sub2api-401/retry` in synchronous completion mode.
5. Preserve Codex reservation, `mark_retrying`, post-run detection, batch evidence, replacement verification, and `cleanup(..., opt_in=True)` for only same-batch locked exact old IDs.

No email-wide remote mutation is permitted.

### ChatGPT2API401

1. Call the existing `ChatGPT2API401Service.detect()` once; an active queue becomes `skipped_busy`.
2. Select only fresh `pending` and retryable stable remote IDs, capped at 5000.
3. Call existing `refresh(remote_ids, opt_in=True, limit=5000)` and use its sanitized `submitted_ids` as the exact ownership set accepted by this round.
4. Keep scheduler phase `waiting_for_queue` until only those submitted IDs reach terminal audit states; a submitted ID missing from the audit is counted as failed, never completed, while skipped or manually queued IDs cannot hold or influence this round.
5. Existing queue workers remain the only path to liveness, exact-ID AT writeback, AT readback, and unchanged RT evidence.

The scheduler never implements token refresh or remote PATCH logic itself.

## State and Security

Public phases: `disabled`, `idle`, `running`, `waiting_for_queue`, `interrupted`.

Public outcomes: `success`, `partial`, `failed`, `skipped_busy`, `interrupted`.

Progress fields: `detected`, `actionable`, `submitted`, `completed`, `failed`, `skipped`.

Scheduler/API/audit data must never contain AT, RT, management keys, passwords, OAuth codes, callback URLs, authorization headers, or upstream credential bodies. Errors are whitespace-normalized, secret-pattern redacted, URL redacted, token-pattern redacted, and capped at 500 characters. Scheduler endpoints use the existing authentication and `no-store`, `Pragma: no-cache`, and `nosniff` headers.

## UI Contract

Both pages expose independent enable controls, 30-minute and 1-hour presets, custom 5–1440 minute input, save state, phase, recent/next timestamps, progress, and bounded error. Pages poll product state every 3 seconds while manual or automatic work is active, every 30 seconds while automatic inspection is enabled but idle, and stop polling when disabled and terminal. Closing the page does not stop server work.

## Required Tests

- Default disabled, immediate enable, completion-relative cadence, interval update, disable/re-enable, single-flight, independent jobs, interrupted restart recovery, exception isolation, persistence, symlink/mode, redaction, and malformed/version fallback.
- API authentication, validation, independent settings, no-store headers, and scheduler projection in product state.
- Sub2API automatic adapter uses fresh unauthorized exact IDs and existing retry/post-detect/exact-cleanup gates.
- ChatGPT2API automatic adapter uses fresh pending exact IDs and existing fixed-five-worker queue/writeback gates.
- Templates contain presets/custom controls, progress/error display, scheduler endpoints, and single-flight polling.
