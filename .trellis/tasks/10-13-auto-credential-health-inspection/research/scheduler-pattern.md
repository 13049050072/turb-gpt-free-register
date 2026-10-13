# Persistent interval scheduler pattern

## Recommendation

Use one small, process-local `CredentialHealthScheduler` with one versioned JSON state file and two independent job records: `sub2api_401` and `chatgpt2api_401`. Each record has its own `enabled`, `interval_minutes`, due time, run state, progress, and bounded error. A coordinator thread owns timing and dispatches due jobs to a bounded executor with at most one active future per job and at most two jobs overall.

This is a good fit for the deployed topology because `Dockerfile:76` starts Gunicorn as one `gthread` worker with 16 request threads. It is not a distributed scheduler. If Gunicorn is changed to multiple workers or the application is replicated, each process would otherwise create its own scheduler; that topology needs a leader lease, database-backed queue, or an external scheduler before this feature may be enabled.

Store both job records in one file rather than two unrelated scheduler files. The lifecycle and persistence mechanism are shared, while each product remains independently configurable and fault-isolated. A failure or long run in one product must not prevent the other product's future from starting.

## Repository evidence

| Concern | Existing pattern | Consequence |
| --- | --- | --- |
| Production topology | `Dockerfile:76` uses `gunicorn --worker-class gthread --workers 1 --threads 16 ... webui.app:create_app()` | A daemon thread is valid under the current single-process contract. Thread locks do not become cross-process locks. |
| App startup | `webui/app.py:264-268` constructs both 401 services inside `create_app()`; `webui/app.py:335-362` performs restart recovery and starts the existing import worker | Construct the scheduler after both services/coordinators exist, and start it beside `start_import_worker()`, after recovery. Do not start it at module import time. |
| Existing daemon | `core/chatgpt2api_import_service.py:427-454` uses a module lock, `threading.Event`, idempotent daemon startup, and `Event.wait(timeout)` | Reuse the lock/event/idempotent-start shape. Use an event so config changes and shutdown can interrupt a long wait. Do not use `time.sleep()` in the scheduler loop. |
| Atomic JSON | `core/sub2api_401_service.py:146-199` resolves symlink targets and writes beside the target with mode `0600` and `os.replace()`; the ChatGPT2API audit additionally uses process-shared path locks and create-exclusive temporary files | Persist a sanitized versioned scheduler document by create-exclusive temp file, flush/`fsync`, mode `0600`, then `os.replace()` beside the resolved target. Serialize all reads/writes with a path-keyed process lock. |
| Data volume | `Dockerfile:52-66` maps named state files from `/app` to `/data`, including both current 401 audit files | Add the scheduler state filename, for example `credential_health_scheduler.json`, to the same symlink list during implementation or use an explicitly configured `/data/...` path. Otherwise settings disappear on container replacement. |
| Sub2API service gate | `core/sub2api_401_service.py:614-640` exposes `select_retryable()` and `mark_retrying()`; detection persists exact-ID evidence; cleanup revalidates the locked old and replacement IDs | Automatic work must select only IDs returned as retryable by the just-completed `detect()`, then use the same retry and cleanup coordinator as the manual route. |
| Sub2API route workflow | `webui/app.py:2809-2947` currently contains reservation, Codex retry dispatch, post-run `detect()`, and exact-ID `cleanup()` in a nested route runner | Extract this orchestration into a reusable application service. Both the manual endpoint and scheduler should call it; the scheduler must not call Flask routes or copy this nested runner. |
| ChatGPT2API queue gate | `core/chatgpt2api_401_service.py:42-87` shares operation locks and queue ownership by audit path; `state()` reports queue progress; `detect()` rejects while queued/refreshing at `core/chatgpt2api_401_service.py:475-487` | Let the scheduler call the service directly. Existing state and exact-ID guards remain authoritative, but a scheduler-level per-job flight gate is still needed for one complete inspection round. |
| ChatGPT2API reuse | `webui/app.py:3026-3058` maps detect and refresh to `ChatGPT2API401Service.detect()` and `.refresh()`; `core/chatgpt2api_401_service.py:707-970` performs pre-liveness evidence checks and exact-ID writeback verification | Automatic work is `detect -> collect retryable pending IDs -> refresh(..., opt_in=True) -> observe queue completion`. Never implement a second liveness or PATCH path. |
| Existing tests | `tests/test_sub2api_401_service.py`, `tests/test_chatgpt2api_401_service.py`, `tests/test_webui_sub2api_401.py`, `tests/test_webui_chatgpt2api_401.py`, and both template tests inject functions, use temporary audit paths, patch service methods, and inspect static HTML/JS | Keep scheduler timing, executor, persistence path, and job adapters injectable. Test state transitions by calling a deterministic `run_due()` seam, not by sleeping. |

## Proposed component boundaries

A suitable module is `core/credential_health_scheduler.py`. Keep scheduling policy separate from credential business logic.

```python
@dataclass(frozen=True)
class InspectionResult:
    outcome: str                 # success | partial | failed | skipped_busy
    detected: int = 0
    actionable: int = 0
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    error: str = ""             # bounded and redacted

class InspectionJob(Protocol):
    def run(self, run_id: str) -> InspectionResult: ...

class CredentialHealthScheduler:
    def start(self) -> None: ...
    def stop(self) -> None: ...             # test/process hygiene, not correctness
    def state(self, job_name: str) -> dict: ...
    def configure(self, job_name: str, *, enabled: bool,
                  interval_minutes: int) -> dict: ...
    def run_due(self, now: datetime | None = None) -> list[str]: ...
```

`run_due()` is the key deterministic seam. It examines persisted due state and submits eligible work without waiting for real time. The production coordinator loop computes the nearest due time, calls `wake_event.wait(timeout)`, then invokes `run_due()`. Tests call `run_due(fake_now)` directly and use an inline or manually controlled executor.

Use a module-level registry keyed by the resolved persistence path, similar to the ChatGPT2API path-keyed runtime. `start()` must be idempotent in a process because app factories, tests, and development reloaders can construct more than one app. Expose the scheduler through `app.extensions["credential_health_scheduler"]` so tests and shutdown hooks can access it without importing a second singleton.

The scheduler should have one coordinator thread and a two-slot executor. Keep `active_future[job_name]`; submit only when it is absent or done. A done callback persists the terminal result and next due time, then wakes the coordinator. Never hold the JSON state lock while performing network calls, OAuth, liveness, or waiting for a product queue.

## Persistent state contract

Recommended shape:

```json
{
  "version": 1,
  "updated_at": "2026-10-13T12:00:00+00:00",
  "jobs": {
    "sub2api_401": {
      "enabled": false,
      "interval_minutes": 30,
      "phase": "disabled",
      "run_id": "",
      "last_started_at": "",
      "last_finished_at": "",
      "next_run_at": "",
      "last_outcome": "",
      "progress": {
        "detected": 0,
        "actionable": 0,
        "submitted": 0,
        "completed": 0,
        "failed": 0,
        "skipped": 0
      },
      "error": ""
    },
    "chatgpt2api_401": {}
  }
}
```

Allowed public phases should be small and explicit: `disabled`, `idle`, `running`, `waiting_for_queue`, and `interrupted`. `last_outcome` should be one of `success`, `partial`, `failed`, `skipped_busy`, or `interrupted`.

Persist configuration and lifecycle transitions, not credentials or per-account payloads. Do not store AT, RT, management keys, authorization headers, OAuth codes, callback URLs, upstream bodies, or exception representations that may contain them. Reuse the existing bounded redaction rules before storing/logging `error`; cap public errors, for example at 500 characters. Counts and timestamps are sufficient for scheduler progress because the existing product state endpoints already expose sanitized per-account evidence.

Use aware UTC timestamps for durable wall-clock fields. A monotonic clock may measure an in-process wait or duration, but it cannot be persisted across restarts. Validate loaded data defensively: unknown versions/fields, invalid timestamps, booleans passed as integers, and intervals outside `5..1440` must not activate work.

## Lifecycle and cadence

### Start and stop

1. `create_app()` constructs both 401 services and reusable workflow coordinators.
2. Existing interrupted DB/queue recovery runs.
3. The scheduler loads and sanitizes its JSON file, reconciles interrupted scheduler state, and starts exactly one daemon coordinator in that process.
4. `stop()` sets a stop event and wakes the loop. It exists for tests and orderly teardown; daemon-thread shutdown is not the durability mechanism.

Do not use Flask `before_request` or browser traffic to start or advance the scheduler. That would make startup nondeterministic and could create races under request concurrency. Do not rely on `atexit` to write the final state; persist `running` before work starts and every terminal transition as it occurs.

### Enable, disable, and interval changes

- Default is disabled with `interval_minutes=30`.
- `false -> true`: atomically persist `enabled=true`, set `next_run_at=now`, then signal the wake event. This gives the required immediate first run.
- `true -> false`: persist disabled and clear `next_run_at`. Do not kill an already-running OAuth/liveness/writeback transaction; let it reach a verified terminal state, but do not schedule another run.
- Interval updates require a real integer, excluding `bool`, in `5..1440`.
- Updating an enabled idle job should set `next_run_at=now + new_interval`; it should not cause an unrequested immediate run.
- Updating a running job changes the interval used after that run finishes; it must not create a second flight.

After a terminal result, schedule `next_run_at=finished_at + interval`. Completion-relative cadence avoids catch-up storms and guarantees spacing when a run takes longer than its interval. If strict wall-clock cadence is later required, that is a separate policy and needs explicit missed-run semantics.

### Restart recovery

On load:

- Disabled jobs remain disabled.
- An enabled idle job with a future `next_run_at` resumes waiting for that time.
- An enabled job whose `next_run_at` is missing or due runs once promptly.
- A persisted `running` or `waiting_for_queue` phase means the process died. Record the previous run as `interrupted`, clear its in-memory ownership, and make one run due immediately if still enabled. Do not replay every missed interval.
- Product recovery remains authoritative: ChatGPT2API already converts persisted queued/refreshing work into retryable failure on service construction; Sub2API reconciliation must re-detect and pass the same exact-ID snapshot gates before any retry or cleanup.

Persist-before-execute ordering is important: write the new `run_id`, `phase=running`, and `last_started_at` before invoking the job. On recovery, never infer that an external mutation succeeded merely because the scheduler had reached `running`; the product services must re-read and verify their own evidence.

## Single-flight and manual-operation interaction

There are two distinct gates:

1. The scheduler keeps one active future per product, preventing timer reentry.
2. A shared product workflow coordinator arbitrates manual and automatic starts.

The second gate is essential. The current Sub2API manual orchestration is nested in the Flask route and uses `_reserve_codex_retry(email)` plus service snapshot gates. Move that orchestration behind one coordinator API that accepts selected exact IDs, worker count, origin, and an optional completion callback/future. The route and automatic adapter must both call it. The future should represent the full `retry -> post-detect -> exact-ID cleanup` round, not merely thread submission.

For Sub2API automatic work:

1. Call `Sub2Api401Service.detect(current_local_statuses)` once.
2. Extract only rows from that result with `retryable=true`, `state=pending`, and health `unauthorized`; pass their exact `account_id` values back through `select_retryable(..., limit=50)`.
3. Submit those selected IDs through the shared Codex retry coordinator, preserving `_reserve_codex_retry(email)`, `mark_retrying()`, the existing post-run `detect()`, and `cleanup(..., opt_in=True)` only for the locked selected IDs that become `cleanup_ready` in the same batch.
4. Treat the coordinator's full completion as the scheduler run completion. Record over-limit, already reserved, stale, ambiguous, and failed-cleanup rows only as bounded counts/errors.

For ChatGPT2API automatic work:

1. Call `ChatGPT2API401Service.detect()` once. If the existing queue is active, classify the automatic attempt as `skipped_busy`; do not overwrite audit state.
2. Select only rows from that fresh result with `retryable=true` and `state=pending`; call `refresh(remote_ids, opt_in=True, limit=CHATGPT2API_401_MAX_ITEMS)`.
3. Regard the scheduler run as `waiting_for_queue` until that submitted set reaches terminal states, or provide a completion future from the existing queue runtime. Do not mark the run successful just because `refresh()` returned HTTP-style acceptance data.
4. Let `_refresh_and_replace()` and `replace()` remain the only liveness/writeback path; they already revalidate old AT/RT hashes, stable remote ID, email uniqueness, exact PATCH acknowledgement, and post-write evidence.

When a manual operation already owns the product coordinator, the automatic run should return `skipped_busy` and schedule the next normal cadence. It should not spin, block request threads, duplicate work, or reset product audit data. The other product remains independently runnable.

## Gunicorn caveat

The current contract is exactly one Gunicorn worker process. `threading.Lock`, path-keyed `RLock`, active-future maps, and module globals coordinate threads only within that process. Atomic `os.replace()` prevents torn JSON but does not provide cross-process single-flight.

Therefore:

- Document one worker as a deployment precondition for the in-process scheduler.
- Emit a clear startup log stating that the scheduler is process-local.
- Do not claim multi-worker correctness from the JSON file.
- If topology changes, disable this in-process scheduler until a single elected owner is provided. Viable upgrades are a database advisory/lease row with expiry and owner ID, a durable queue worker, or an external cron/system scheduler calling an authenticated internal command. Merely adding a file lock does not solve multi-host failover.

Gunicorn graceful restart also creates a short overlap between old and new processes. Under the current one-worker assumption, product exact-ID/evidence gates reduce unsafe mutation risk but do not constitute scheduler leadership. Deployment should stop accepting new scheduler work before intentional rolling multi-process overlap, or the future leader mechanism must cover it.

## API and UI contract

Use independent endpoints so each page manages only its own job while the backend still shares one scheduler:

```text
GET /api/sub2api-401/scheduler
PUT /api/sub2api-401/scheduler       {"enabled": true, "interval_minutes": 30}
GET /api/chatgpt2api-401/scheduler
PUT /api/chatgpt2api-401/scheduler   {"enabled": true, "interval_minutes": 60}
```

Alternatively, embed the same sanitized scheduler projection in the existing product `state` response. The most useful arrangement is: config endpoints for validation/update, plus a `scheduler` member in each existing `/state` response so one page poll updates both product and scheduler progress.

The public scheduler projection should contain only:

```json
{
  "enabled": true,
  "interval_minutes": 30,
  "phase": "running",
  "run_id": "bounded-random-id",
  "last_started_at": "...",
  "last_finished_at": "...",
  "next_run_at": "...",
  "last_outcome": "partial",
  "progress": {
    "detected": 12,
    "actionable": 3,
    "submitted": 3,
    "completed": 2,
    "failed": 1,
    "skipped": 0
  },
  "error": "bounded redacted message"
}
```

Validation rules:

- Body must be a JSON object.
- `enabled` must be a literal boolean.
- `interval_minutes` must be an integer but not `bool`, in `5..1440`.
- Reject unknown product/job names; clients never choose arbitrary persistence keys.
- Return `400` for invalid input and preserve the previous configuration.
- Apply the same authentication and `Cache-Control: no-store, max-age=0`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff` headers already used by both 401 APIs.

Each page should use a compact operational row/card with:

- a real enable switch;
- a segmented `30 分钟` / `1 小时` / `自定义` selector;
- a numeric custom input with `min=5`, `max=1440`, and integer validation;
- save command and saving/error state;
- phase, last start/finish/result, next run, progress counts, and bounded error.

The server remains authoritative. A page close changes nothing. On load, fetch state once. While either the manual workflow or automatic phase is active, keep the existing one-request-at-a-time 3000 ms poll. While enabled but idle, use `next_run_at` to schedule a refresh near the due time (or a low-frequency 30-second status refresh), rather than running expensive detection from the browser. The browser must never trigger periodic `/detect`, `/retry`, or `/refresh` calls.

## Deterministic tests without sleeping

Build tests around injected dependencies:

- `now_fn() -> aware UTC datetime`;
- executor interface (`submit`) with an inline executor and a manually completable future;
- persistence path under `TemporaryDirectory`;
- `InspectionJob` fakes recording run IDs and returning controlled results;
- wake/wait abstraction only for the small lifecycle-loop test.

Core scheduler tests should call `configure()` and `run_due(fake_now)` directly:

1. Default disabled: advancing fake time submits nothing.
2. Enable: persists state and one `run_due(now)` submits immediately.
3. Cadence: before due submits nothing; at due submits once; next due is based on finish time.
4. Interval validation: reject `True`, `4`, `1441`, float, string, and missing fields without changing state; accept `5`, `30`, `60`, and `1440`.
5. Single-flight: hold a manual future incomplete, advance beyond several intervals, and prove no second submit occurs.
6. Independent jobs: block Sub2API and prove ChatGPT2API can start and finish.
7. Disable during run: current future may finish, but no next run is scheduled.
8. Re-enable: creates exactly one immediate run.
9. Restart: instantiate a second scheduler from the same file; future due time is preserved, overdue runs once, and persisted running becomes interrupted then due once.
10. Failure: an adapter exception becomes bounded `failed` state and does not kill the coordinator or prevent the other job.
11. Persistence: atomic replace, symlink preservation, mode `0600`, malformed-file fallback, version handling, and no seeded secret text in file/API/log output.
12. Idempotent startup: repeated `start()` for the same resolved path creates one coordinator thread.

Adapter/coordinator tests should reuse existing fake services and assert call order:

- Sub2API: one detect; only fresh unauthorized/retryable exact IDs selected; shared reservation; mark retrying; Codex completion; post-detect; cleanup only same-batch `cleanup_ready` old IDs; no email-wide mutation.
- ChatGPT2API: one detect; only fresh pending/retryable stable IDs passed to `refresh(..., opt_in=True)`; scheduler remains active until submitted queue work is terminal; busy queue is skipped; no direct liveness or patch implementation in scheduler.
- Manual/automatic contention: whichever acquires the shared product coordinator first wins; the other receives busy/skipped without duplicate account work.

API tests should patch the scheduler through `app.extensions`, verify independent settings, validation, immediate wake, persistence across a new app/scheduler instance, authentication, no-store headers, and redacted errors. Template tests should assert controls, preset/custom behavior, the configuration endpoints, scheduler rendering, and 3000 ms single-flight polling only while work is active. Extract both inline scripts and run `node --check` as required by the task definition.

No test needs `time.sleep()`. A single lifecycle test can give a fake `wait(timeout)` implementation that records the computed timeout and returns on demand; all policy tests remain synchronous.

## Patterns to avoid

- APScheduler or Celery solely for two interval jobs: they add dependency and operational surface without solving this repository's process ownership unless a shared job store/broker is also deployed.
- Flask request hooks or browser `setInterval` calling mutation endpoints: lifecycle would depend on traffic/page presence and could duplicate work.
- Calling the application's own HTTP endpoints from the scheduler: this duplicates authentication/error mapping and obscures transaction ownership.
- Copying the nested Sub2API route runner or ChatGPT2API liveness/writeback logic into the scheduler: fixes would drift and exact-ID/evidence gates could be bypassed.
- Holding scheduler locks during network/OAuth work: config and state reads would block for minutes.
- Fixed-rate catch-up loops: a long run could immediately launch repeated overdue rounds.
- Treating enqueue acceptance as run completion: ChatGPT2API failures happen asynchronously after acceptance.
- Assuming atomic JSON is a distributed mutex: it prevents torn writes, not duplicate execution.

## Implementation decision summary

- One scheduler service and one versioned JSON file; two independent records and flights.
- One daemon coordinator plus a two-slot executor under the current one-worker Gunicorn topology.
- Event-driven waits, completion-relative cadence, immediate first run, and no catch-up replay.
- Persist-before-execute and explicit interrupted-run recovery.
- Shared orchestration coordinators for both manual and automatic paths; the scheduler contains no credential business logic.
- Existing Sub2API exact-ID retry/verification/cleanup and ChatGPT2API liveness/exact-ID writeback services remain authoritative.
- Deterministic `run_due(fake_now)` tests; no real sleeping.
- Multi-process scheduling remains explicitly unsupported until leadership is added.
