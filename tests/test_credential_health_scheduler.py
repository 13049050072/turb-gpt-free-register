# -*- coding: utf-8 -*-
import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.credential_health_scheduler import CredentialHealthScheduler, InspectionResult


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class _ManualExecutor:
    def __init__(self):
        self.submissions = []

    def submit(self, fn, *args):
        future = Future()
        self.submissions.append((future, fn, args))
        return future

    def run(self, index=0):
        future, fn, args = self.submissions[index]
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


class CredentialHealthSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "scheduler.json"
        self.now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        self.clock = _Clock(self.now)
        self.executor = _ManualExecutor()
        self.calls = {"sub2api_401": [], "chatgpt2api_401": []}

        def make_job(name):
            def run(run_id):
                self.calls[name].append(run_id)
                return InspectionResult(outcome="success", detected=2, actionable=1, submitted=1, completed=1)
            return run

        self.scheduler = CredentialHealthScheduler(
            {name: make_job(name) for name in self.calls},
            state_path=self.path,
            now_fn=self.clock,
            executor=self.executor,
        )
        self.addCleanup(self.scheduler.stop)

    def test_default_disabled_and_enable_runs_immediately(self):
        self.assertFalse(self.scheduler.state("sub2api_401")["enabled"])
        self.assertEqual([], self.scheduler.run_due(self.now + timedelta(days=1)))
        configured = self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        self.assertEqual(self.now.isoformat(), configured["next_run_at"])
        self.assertEqual(["sub2api_401"], self.scheduler.run_due(self.now))
        self.executor.run()
        state = self.scheduler.state("sub2api_401")
        self.assertEqual("success", state["last_outcome"])
        self.assertEqual((self.now + timedelta(minutes=30)).isoformat(), state["next_run_at"])
        self.assertEqual(1, state["progress"]["completed"])

    def test_completion_relative_cadence_and_single_flight(self):
        self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        self.assertEqual(["sub2api_401"], self.scheduler.run_due(self.now))
        self.assertEqual([], self.scheduler.run_due(self.now + timedelta(hours=3)))
        self.clock.value = self.now + timedelta(minutes=7)
        self.executor.run()
        state = self.scheduler.state("sub2api_401")
        self.assertEqual((self.clock.value + timedelta(minutes=30)).isoformat(), state["next_run_at"])
        self.assertEqual([], self.scheduler.run_due(self.clock.value + timedelta(minutes=29)))

    def test_jobs_are_independent(self):
        for name in self.calls:
            self.scheduler.configure(name, enabled=True, interval_minutes=30)
        self.assertEqual(list(self.calls), self.scheduler.run_due(self.now))
        self.executor.run(1)
        self.assertEqual("running", self.scheduler.state("sub2api_401")["phase"])
        self.assertEqual("idle", self.scheduler.state("chatgpt2api_401")["phase"])

    def test_disable_during_run_and_reenable(self):
        self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        self.scheduler.run_due(self.now)
        state = self.scheduler.configure("sub2api_401", enabled=False, interval_minutes=60)
        self.assertFalse(state["enabled"])
        self.executor.run()
        state = self.scheduler.state("sub2api_401")
        self.assertEqual("disabled", state["phase"])
        self.assertEqual("", state["next_run_at"])
        self.clock.value += timedelta(minutes=1)
        state = self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=60)
        self.assertEqual(self.clock.value.isoformat(), state["next_run_at"])

    def test_idle_interval_update_reschedules_without_immediate_run(self):
        self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        self.scheduler.run_due(self.now)
        self.executor.run()
        self.clock.value += timedelta(minutes=2)
        state = self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=60)
        self.assertEqual((self.clock.value + timedelta(minutes=60)).isoformat(), state["next_run_at"])

    def test_running_phase_is_not_rescheduled_before_future_registration(self):
        self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        with self.scheduler._lock:
            job = self.scheduler._state["jobs"]["sub2api_401"]
            job["phase"] = "running"
            job["run_id"] = "claimed-run"
            job["next_run_at"] = ""
        state = self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=60)
        self.assertEqual("running", state["phase"])
        self.assertEqual("", state["next_run_at"])

    def test_validation_preserves_previous_state(self):
        before = self.scheduler.state("sub2api_401")
        for enabled, interval in ((1, 30), (True, True), (True, 4), (True, 1441), (True, 30.0), (True, "30")):
            with self.subTest(enabled=enabled, interval=interval), self.assertRaises(ValueError):
                self.scheduler.configure("sub2api_401", enabled=enabled, interval_minutes=interval)
        self.assertEqual(before, self.scheduler.state("sub2api_401"))
        with self.assertRaises(KeyError):
            self.scheduler.state("unknown")

    def test_restart_recovers_interrupted_job_as_due(self):
        payload = {
            "version": 1,
            "jobs": {
                "sub2api_401": {
                    "enabled": True, "interval_minutes": 30, "phase": "waiting_for_queue",
                    "run_id": "old", "last_started_at": self.now.isoformat(), "next_run_at": "",
                },
                "chatgpt2api_401": {"enabled": False, "interval_minutes": 30, "phase": "disabled"},
            },
        }
        self.scheduler.stop()
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        recovered = CredentialHealthScheduler(
            {name: (lambda _run_id: InspectionResult(outcome="success")) for name in self.calls},
            state_path=self.path, now_fn=self.clock, executor=_ManualExecutor(),
        )
        state = recovered.state("sub2api_401")
        self.assertEqual("interrupted", state["last_outcome"])
        self.assertEqual("idle", state["phase"])
        self.assertEqual(self.now.isoformat(), state["next_run_at"])

    def test_exception_is_redacted_and_other_job_can_finish(self):
        def secret_failure(_run_id):
            raise RuntimeError("access_token=very-secret-value callback=https://secret.example/cb")

        self.scheduler.stop()
        executor = _ManualExecutor()
        scheduler = CredentialHealthScheduler(
            {
                "sub2api_401": secret_failure,
                "chatgpt2api_401": lambda _run_id: InspectionResult(outcome="success", completed=1),
            },
            state_path=self.path, now_fn=self.clock, executor=executor,
        )
        for name in self.calls:
            scheduler.configure(name, enabled=True, interval_minutes=30)
        scheduler.run_due(self.now)
        executor.run(0)
        executor.run(1)
        failed = scheduler.state("sub2api_401")
        self.assertEqual("failed", failed["last_outcome"])
        self.assertNotIn("very-secret-value", failed["error"])
        self.assertNotIn("secret.example", failed["error"])
        self.assertEqual("success", scheduler.state("chatgpt2api_401")["last_outcome"])
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("very-secret-value", persisted)

    def test_progress_update_requires_matching_run(self):
        self.scheduler.configure("chatgpt2api_401", enabled=True, interval_minutes=30)
        self.scheduler.run_due(self.now)
        run_id = self.scheduler.state("chatgpt2api_401")["run_id"]
        self.assertFalse(self.scheduler.update_progress("chatgpt2api_401", "wrong", submitted=9))
        self.assertTrue(self.scheduler.update_progress(
            "chatgpt2api_401", run_id, phase="waiting_for_queue", detected=4, submitted=2
        ))
        state = self.scheduler.state("chatgpt2api_401")
        self.assertEqual("waiting_for_queue", state["phase"])
        self.assertEqual(2, state["progress"]["submitted"])

    def test_symlink_target_is_preserved_and_mode_is_private(self):
        real = Path(self.tmp.name) / "data" / "scheduler.json"
        real.parent.mkdir()
        link = Path(self.tmp.name) / "scheduler-link.json"
        link.symlink_to(real)
        scheduler = CredentialHealthScheduler(
            {name: (lambda _run_id: InspectionResult(outcome="success")) for name in self.calls},
            state_path=link, now_fn=self.clock, executor=_ManualExecutor(),
        )
        scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        self.assertTrue(link.is_symlink())
        self.assertTrue(real.exists())
        self.assertEqual(0o600, stat.S_IMODE(real.stat().st_mode))

    def test_malformed_and_unknown_version_fall_back_disabled(self):
        self.scheduler.stop()
        for content in ("not-json", json.dumps({"version": 99, "jobs": {"sub2api_401": {"enabled": True}}})):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                scheduler = CredentialHealthScheduler(
                    {name: (lambda _run_id: InspectionResult(outcome="success")) for name in self.calls},
                    state_path=self.path, now_fn=self.clock, executor=_ManualExecutor(),
                )
                self.assertFalse(scheduler.state("sub2api_401")["enabled"])
                scheduler.stop()

    def test_concurrent_same_path_construction_cannot_reset_persisted_configuration(self):
        path = Path(self.tmp.name) / "concurrent-singleton.json"
        barrier = threading.Barrier(2)

        def construct_and_configure(interval):
            barrier.wait()
            scheduler = CredentialHealthScheduler(
                {name: (lambda _run_id: InspectionResult(outcome="success")) for name in self.calls},
                state_path=path,
                now_fn=self.clock,
                executor=_ManualExecutor(),
            )
            scheduler.configure("sub2api_401", enabled=True, interval_minutes=interval)
            return scheduler

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(construct_and_configure, 30)
            second_future = pool.submit(construct_and_configure, 60)
            first = first_future.result(timeout=3)
            second = second_future.result(timeout=3)
        self.addCleanup(first.stop)
        self.assertIs(first, second)
        state = first.state("sub2api_401")
        self.assertTrue(state["enabled"])
        self.assertIn(state["interval_minutes"], (30, 60))
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(stored["jobs"]["sub2api_401"]["enabled"])
        self.assertEqual(state["interval_minutes"], stored["jobs"]["sub2api_401"]["interval_minutes"])

    def test_same_state_path_reuses_one_process_runtime(self):
        second = CredentialHealthScheduler(
            {name: (lambda _run_id: InspectionResult(outcome="success")) for name in self.calls},
            state_path=self.path,
            now_fn=self.clock,
            executor=_ManualExecutor(),
        )
        self.assertIs(self.scheduler, second)
        self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        second.configure("chatgpt2api_401", enabled=True, interval_minutes=60)
        self.assertTrue(self.scheduler.state("sub2api_401")["enabled"])
        self.assertTrue(second.state("chatgpt2api_401")["enabled"])
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(persisted["jobs"]["sub2api_401"]["enabled"])
        self.assertTrue(persisted["jobs"]["chatgpt2api_401"]["enabled"])

    def test_short_and_quoted_secrets_are_redacted_from_result_errors(self):
        secret_error = (
            'Authorization: Bearer short-secret authorization=Basic c2hvcnQ= '
            '"access_token": "abc123" refresh_token=rt123 '
            'proxy=FAKE_PROXY_USER:FAKE_PROXY_PASS@proxy.example.test:8080 '
            'upstream_proxy=FAKE_UPSTREAM_USER:FAKE_UPSTREAM_PASS@upstream.example.test:9000 '
            'socks5://FAKE_SOCKS_USER:FAKE_SOCKS_PASS@socks.example.test:1080 '
            'callback=https://secret.example/cb?token=hidden status=token expired'
        )
        self.scheduler._jobs["sub2api_401"] = lambda _run_id: InspectionResult(
            outcome="failed", failed=1, error=secret_error
        )
        self.scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        self.scheduler.run_due(self.now)
        self.executor.run()
        error = self.scheduler.state("sub2api_401")["error"]
        for secret in (
            "short-secret", "c2hvcnQ=", "abc123", "rt123", "secret.example", "hidden",
            "FAKE_PROXY_USER", "FAKE_PROXY_PASS", "FAKE_UPSTREAM_USER", "FAKE_UPSTREAM_PASS",
            "FAKE_SOCKS_USER", "FAKE_SOCKS_PASS",
        ):
            self.assertNotIn(secret, error)
        self.assertIn("token expired", error)
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("abc123", persisted)
        self.assertNotIn("short-secret", persisted)

    def test_stop_waits_for_owned_executor_before_registry_replacement(self):
        path = Path(self.tmp.name) / "stop-lifecycle.json"
        started = threading.Event()
        release = threading.Event()
        stopped = threading.Event()

        def blocking_job(_run_id):
            started.set()
            release.wait()
            return InspectionResult(outcome="success", completed=1)

        scheduler = CredentialHealthScheduler(
            {
                "sub2api_401": blocking_job,
                "chatgpt2api_401": lambda _run_id: InspectionResult(outcome="success"),
            },
            state_path=path,
            now_fn=self.clock,
        )
        scheduler.configure("sub2api_401", enabled=True, interval_minutes=30)
        scheduler.run_due(self.now)
        self.assertTrue(started.wait(2))

        stop_thread = threading.Thread(target=lambda: (scheduler.stop(), stopped.set()))
        stop_thread.start()
        self.assertFalse(stopped.wait(0.05))
        release.set()
        self.assertTrue(stopped.wait(2))
        stop_thread.join(timeout=2)

        replacement = CredentialHealthScheduler(
            {name: (lambda _run_id: InspectionResult(outcome="success")) for name in self.calls},
            state_path=path,
            now_fn=self.clock,
            executor=_ManualExecutor(),
        )
        self.addCleanup(replacement.stop)
        state = replacement.state("sub2api_401")
        self.assertEqual("success", state["last_outcome"])
        self.assertEqual(1, state["progress"]["completed"])

    def test_start_is_idempotent_while_disabled(self):
        self.scheduler.start()
        self.scheduler.start()
        self.assertTrue(self.scheduler._started)
        self.assertIsNone(self.scheduler._coordinator)


if __name__ == "__main__":
    unittest.main()
