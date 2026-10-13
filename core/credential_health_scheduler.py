# -*- coding: utf-8 -*-
"""Persistence-backed process-local scheduler for credential health inspection."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

JOB_NAMES = ("sub2api_401", "chatgpt2api_401")
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440
DEFAULT_INTERVAL_MINUTES = 30
_ALLOWED_PHASES = {"disabled", "idle", "running", "waiting_for_queue", "interrupted"}
_ALLOWED_OUTCOMES = {"", "success", "partial", "failed", "skipped_busy", "interrupted"}
_PROGRESS_KEYS = ("detected", "actionable", "submitted", "completed", "failed", "skipped")
_SECRET_NAME = (
    r"access[_ -]?token|refresh[_ -]?token|id[_ -]?token|api[_ -]?key|password|secret|"
    r"callback(?:_url)?|oauth[_ -]?code|authorization"
)
_QUOTED_SECRET_RE = re.compile(
    rf"(?is)([\"']?(?:{_SECRET_NAME})[\"']?\s*[:=]\s*)([\"']).*?\2"
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?i)([\"']?(?:{_SECRET_NAME})[\"']?\s*[:=]\s*)(?:(?:bearer|basic)\s+)?[^\s,;]+"
)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_URL_RE = re.compile(r"https?://[^\s,;]+", re.I)
_TOKENISH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{20,}(?:\.[A-Za-z0-9_+/=-]{8,})*")
_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, "CredentialHealthScheduler"] = {}


@dataclass(frozen=True)
class InspectionResult:
    outcome: str
    detected: int = 0
    actionable: int = 0
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    error: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime:
    value = value or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str:
    return _aware_utc(value).isoformat() if value is not None else ""


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return _aware_utc(parsed)


def _bounded_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(1_000_000, parsed))


def _safe_error(value: object) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ").split())
    text = _QUOTED_SECRET_RE.sub(r"\1\2[redacted]\2", text)
    text = _UNQUOTED_SECRET_RE.sub(r"\1[redacted]", text)
    text = _AUTH_SCHEME_RE.sub("authorization [redacted]", text)
    text = _URL_RE.sub("[url-redacted]", text)
    text = _TOKENISH_RE.sub("[token-redacted]", text)
    return text[:500]


def _state_registry_key(path: str | Path | None) -> str:
    state_path = Path(path) if path is not None else Path(__file__).resolve().parent.parent / "credential_health_scheduler.json"
    try:
        if state_path.is_symlink():
            state_path = state_path.resolve(strict=False)
    except OSError:
        pass
    return str(state_path.resolve(strict=False))


def _default_progress() -> dict[str, int]:
    return {key: 0 for key in _PROGRESS_KEYS}


def _default_job() -> dict:
    return {
        "enabled": False,
        "interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "phase": "disabled",
        "run_id": "",
        "last_started_at": "",
        "last_finished_at": "",
        "next_run_at": "",
        "last_outcome": "",
        "progress": _default_progress(),
        "error": "",
    }


class CredentialHealthScheduler:
    """Run two independent credential-health jobs in one process."""

    def __new__(cls, jobs, *, state_path=None, **_kwargs):
        registry_key = _state_registry_key(state_path)
        with _REGISTRY_LOCK:
            existing = _REGISTRY.get(registry_key)
            if existing is not None and not getattr(existing, "_stopped", False):
                return existing
            instance = super().__new__(cls)
            instance._registry_key = registry_key
            instance._constructor_lock = threading.RLock()
            _REGISTRY[registry_key] = instance
            return instance

    def __init__(
        self,
        jobs: Mapping[str, Callable[[str], InspectionResult | dict]],
        *,
        state_path: str | Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
        executor=None,
        event_factory: Callable[[], threading.Event] = threading.Event,
    ) -> None:
        with self._constructor_lock:
            if getattr(self, "_initialized", False):
                return
            missing = [name for name in JOB_NAMES if not callable(jobs.get(name))]
            if missing:
                with _REGISTRY_LOCK:
                    if _REGISTRY.get(self._registry_key) is self:
                        _REGISTRY.pop(self._registry_key, None)
                raise ValueError(f"missing scheduler jobs: {', '.join(missing)}")
            self._jobs = {name: jobs[name] for name in JOB_NAMES}
            self._state_path = Path(state_path) if state_path is not None else Path(__file__).resolve().parent.parent / "credential_health_scheduler.json"
            self._now_fn = now_fn or _utc_now
            self._lock = threading.RLock()
            self._wake_event = event_factory()
            self._stop_event = event_factory()
            self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="credential-health")
            self._owns_executor = executor is None
            self._active: dict[str, Future] = {}
            self._coordinator: threading.Thread | None = None
            self._started = False
            self._stopping = False
            self._stopped = False
            self._state = self._load_state()
            if self._reconcile_interrupted():
                self._persist_locked()
            self._initialized = True

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _target_path(self) -> Path:
        try:
            if self._state_path.is_symlink():
                return self._state_path.resolve(strict=False)
        except OSError:
            pass
        return self._state_path

    def _sanitize_job(self, raw: object) -> dict:
        default = _default_job()
        if not isinstance(raw, dict):
            return default
        enabled = raw.get("enabled") if type(raw.get("enabled")) is bool else False
        interval = raw.get("interval_minutes")
        if isinstance(interval, bool) or not isinstance(interval, int) or not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
            interval = DEFAULT_INTERVAL_MINUTES
        phase = str(raw.get("phase") or ("idle" if enabled else "disabled"))
        if phase not in _ALLOWED_PHASES:
            phase = "idle" if enabled else "disabled"
        if not enabled and phase not in {"running", "waiting_for_queue"}:
            phase = "disabled"
        outcome = str(raw.get("last_outcome") or "")
        if outcome not in _ALLOWED_OUTCOMES:
            outcome = ""
        progress_raw = raw.get("progress") if isinstance(raw.get("progress"), dict) else {}
        return {
            "enabled": enabled,
            "interval_minutes": interval,
            "phase": phase,
            "run_id": str(raw.get("run_id") or "")[:64],
            "last_started_at": _iso(_parse_time(raw.get("last_started_at"))) if _parse_time(raw.get("last_started_at")) else "",
            "last_finished_at": _iso(_parse_time(raw.get("last_finished_at"))) if _parse_time(raw.get("last_finished_at")) else "",
            "next_run_at": _iso(_parse_time(raw.get("next_run_at"))) if _parse_time(raw.get("next_run_at")) else "",
            "last_outcome": outcome,
            "progress": {key: _bounded_count(progress_raw.get(key)) for key in _PROGRESS_KEYS},
            "error": _safe_error(raw.get("error")),
        }

    def _load_state(self) -> dict:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            payload = {}
        if not isinstance(payload, dict) or payload.get("version") != 1:
            jobs = {}
        else:
            jobs = payload.get("jobs") if isinstance(payload.get("jobs"), dict) else {}
        return {
            "version": 1,
            "updated_at": str(payload.get("updated_at") or "") if isinstance(payload, dict) else "",
            "jobs": {name: self._sanitize_job(jobs.get(name)) for name in JOB_NAMES},
        }

    def _reconcile_interrupted(self) -> bool:
        now = _aware_utc(self._now_fn())
        changed = False
        for job in self._state["jobs"].values():
            if job["phase"] not in {"running", "waiting_for_queue"}:
                if job["enabled"] and not _parse_time(job["next_run_at"]):
                    job["phase"] = "idle"
                    job["next_run_at"] = _iso(now)
                    changed = True
                continue
            job["phase"] = "idle" if job["enabled"] else "disabled"
            job["last_outcome"] = "interrupted"
            job["last_finished_at"] = _iso(now)
            job["next_run_at"] = _iso(now) if job["enabled"] else ""
            job["error"] = "上次自动巡检因服务重启中断，已安排重新巡检" if job["enabled"] else "上次自动巡检因服务重启中断"
            changed = True
        return changed

    def _persist_locked(self) -> None:
        target = self._target_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = _iso(_aware_utc(self._now_fn()))
        payload = json.dumps(self._state, ensure_ascii=False, indent=2) + "\n"
        temp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        fd = -1
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            try:
                dir_fd = os.open(target.parent, os.O_RDONLY)
            except OSError:
                dir_fd = -1
            if dir_fd >= 0:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def state(self, job_name: str) -> dict:
        if job_name not in JOB_NAMES:
            raise KeyError(job_name)
        with self._lock:
            return copy.deepcopy(self._state["jobs"][job_name])

    def configure(self, job_name: str, *, enabled: bool, interval_minutes: int) -> dict:
        if job_name not in JOB_NAMES:
            raise KeyError(job_name)
        if type(enabled) is not bool:
            raise ValueError("enabled 必须是字面量布尔值")
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int):
            raise ValueError("interval_minutes 必须是整数")
        if not MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES:
            raise ValueError("interval_minutes 必须在 5 到 1440 之间")
        now = _aware_utc(self._now_fn())
        with self._lock:
            job = self._state["jobs"][job_name]
            was_enabled = job["enabled"]
            active = self._active.get(job_name)
            running = (
                (active is not None and not active.done())
                or job["phase"] in {"running", "waiting_for_queue"}
            )
            interval_changed = job["interval_minutes"] != interval_minutes
            job["enabled"] = enabled
            job["interval_minutes"] = interval_minutes
            if not enabled:
                job["next_run_at"] = ""
                if not running:
                    job["phase"] = "disabled"
            elif not was_enabled:
                if not running:
                    job["phase"] = "idle"
                job["next_run_at"] = _iso(now)
                job["error"] = ""
            elif interval_changed and not running:
                job["phase"] = "idle"
                job["next_run_at"] = _iso(now + timedelta(minutes=interval_minutes))
            elif not running and job["phase"] == "disabled":
                job["phase"] = "idle"
                job["next_run_at"] = job["next_run_at"] or _iso(now)
            self._persist_locked()
            result = copy.deepcopy(job)
        if self._started and enabled:
            self._ensure_coordinator()
        self._wake_event.set()
        return result

    def update_progress(self, job_name: str, run_id: str, *, phase: str | None = None, **counts: int) -> bool:
        if job_name not in JOB_NAMES:
            return False
        if phase is not None and phase not in {"running", "waiting_for_queue"}:
            raise ValueError("invalid running phase")
        with self._lock:
            job = self._state["jobs"][job_name]
            if job["run_id"] != run_id or job["phase"] not in {"running", "waiting_for_queue"}:
                return False
            if phase is not None:
                job["phase"] = phase
            for key, value in counts.items():
                if key in _PROGRESS_KEYS:
                    job["progress"][key] = _bounded_count(value)
            self._persist_locked()
        self._wake_event.set()
        return True

    def _normalize_result(self, result: InspectionResult | dict | None) -> InspectionResult:
        if isinstance(result, InspectionResult):
            candidate = result
        elif isinstance(result, dict):
            candidate = InspectionResult(
                outcome=str(result.get("outcome") or "success"),
                detected=_bounded_count(result.get("detected")),
                actionable=_bounded_count(result.get("actionable")),
                submitted=_bounded_count(result.get("submitted")),
                completed=_bounded_count(result.get("completed")),
                failed=_bounded_count(result.get("failed")),
                skipped=_bounded_count(result.get("skipped")),
                error=_safe_error(result.get("error")),
            )
        else:
            candidate = InspectionResult(outcome="success")
        outcome = candidate.outcome if candidate.outcome in _ALLOWED_OUTCOMES - {"", "interrupted"} else "failed"
        return InspectionResult(
            outcome=outcome,
            detected=_bounded_count(candidate.detected),
            actionable=_bounded_count(candidate.actionable),
            submitted=_bounded_count(candidate.submitted),
            completed=_bounded_count(candidate.completed),
            failed=_bounded_count(candidate.failed),
            skipped=_bounded_count(candidate.skipped),
            error=_safe_error(candidate.error),
        )

    def _execute(self, job_name: str, run_id: str):
        return self._jobs[job_name](run_id)

    def _complete(self, job_name: str, run_id: str, future: Future) -> None:
        try:
            result = self._normalize_result(future.result())
        except Exception as exc:
            result = InspectionResult(
                outcome="failed",
                failed=1,
                error=f"{type(exc).__name__}: 自动巡检执行失败",
            )
        finished = _aware_utc(self._now_fn())
        with self._lock:
            job = self._state["jobs"][job_name]
            if job["run_id"] != run_id:
                self._active.pop(job_name, None)
                return
            job["last_finished_at"] = _iso(finished)
            job["last_outcome"] = result.outcome
            job["progress"] = {key: _bounded_count(asdict(result).get(key)) for key in _PROGRESS_KEYS}
            job["error"] = _safe_error(result.error)
            if job["enabled"]:
                job["phase"] = "idle"
                job["next_run_at"] = _iso(finished + timedelta(minutes=job["interval_minutes"]))
            else:
                job["phase"] = "disabled"
                job["next_run_at"] = ""
            self._active.pop(job_name, None)
            self._persist_locked()
        self._wake_event.set()

    def run_due(self, now: datetime | None = None) -> list[str]:
        current = _aware_utc(now or self._now_fn())
        submissions: list[tuple[str, str]] = []
        with self._lock:
            for job_name in JOB_NAMES:
                job = self._state["jobs"][job_name]
                active = self._active.get(job_name)
                if active is not None and not active.done():
                    continue
                due = _parse_time(job["next_run_at"])
                if not job["enabled"] or due is None or due > current:
                    continue
                run_id = uuid.uuid4().hex[:16]
                job.update({
                    "phase": "running",
                    "run_id": run_id,
                    "last_started_at": _iso(current),
                    "last_finished_at": "",
                    "next_run_at": "",
                    "last_outcome": "",
                    "progress": _default_progress(),
                    "error": "",
                })
                self._persist_locked()
                submissions.append((job_name, run_id))

        started: list[str] = []
        for job_name, run_id in submissions:
            try:
                future = self._executor.submit(self._execute, job_name, run_id)
            except Exception as exc:
                failed = Future()
                failed.set_exception(exc)
                self._complete(job_name, run_id, failed)
                continue
            with self._lock:
                self._active[job_name] = future
            future.add_done_callback(lambda done, name=job_name, rid=run_id: self._complete(name, rid, done))
            started.append(job_name)
        return started

    def _next_timeout(self) -> float | None:
        now = _aware_utc(self._now_fn())
        with self._lock:
            due_times = [
                parsed for name in JOB_NAMES
                if self._state["jobs"][name]["enabled"] and name not in self._active
                for parsed in [_parse_time(self._state["jobs"][name]["next_run_at"])]
                if parsed is not None
            ]
        if not due_times:
            return None
        return max(0.0, min((due - now).total_seconds() for due in due_times))

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            timeout = self._next_timeout()
            self._wake_event.wait(timeout)
            self._wake_event.clear()
            if self._stop_event.is_set():
                return
            self.run_due()

    def _ensure_coordinator(self) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            if self._coordinator is not None and self._coordinator.is_alive():
                return
            self._coordinator = threading.Thread(target=self._loop, name="credential-health-scheduler", daemon=True)
            self._coordinator.start()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            enabled = any(self._state["jobs"][name]["enabled"] for name in JOB_NAMES)
        if enabled:
            self._ensure_coordinator()
        self._wake_event.set()

    def stop(self) -> None:
        with self._lock:
            if self._stopped or self._stopping:
                return
            self._stopping = True
        self._stop_event.set()
        self._wake_event.set()
        coordinator = self._coordinator
        if coordinator is not None and coordinator.is_alive() and coordinator is not threading.current_thread():
            coordinator.join(timeout=2)
        if self._owns_executor:
            # An orderly in-process restart must not let callbacks from this
            # runtime overwrite the replacement scheduler's persisted state.
            self._executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._stopped = True
            self._stopping = False
        with _REGISTRY_LOCK:
            if _REGISTRY.get(self._registry_key) is self:
                _REGISTRY.pop(self._registry_key, None)


__all__ = [
    "CredentialHealthScheduler",
    "InspectionResult",
    "JOB_NAMES",
    "MIN_INTERVAL_MINUTES",
    "MAX_INTERVAL_MINUTES",
]
