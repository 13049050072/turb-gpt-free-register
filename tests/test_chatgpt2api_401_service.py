# -*- coding: utf-8 -*-
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.chatgpt2api_401_service import (
    CHATGPT2API_401_WORKERS,
    ChatGPT2API401BusyError,
    ChatGPT2API401Service,
)


class ChatGPT2API401ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.audit_path = Path(self.temp_dir.name) / "chatgpt2api_401_audit.json"
        self.remote = [{
            "remote_id": "remote-old",
            "email": "one@example.com",
            "invalid": True,
            "reason": "HTTP 401 unauthorized",
            "access_token": "remote-old-secret",
            "password": "password-secret",
            "refresh_token": "refresh-secret",
            "client_id": "client-secret",
            "last_refresh": "2026-10-12T00:00:00Z",
        }]
        self.local = [{
            "id": 7,
            "email": "one@example.com",
            "access_token": "local-old-secret",
        }]
        self.patches = []
        self.liveness_calls = []

    def snapshot(self, remote_id):
        row = next(row for row in self.remote if row["remote_id"] == remote_id)
        return dict(row)

    def patch_remote(self, remote_id, access_token):
        self.patches.append((remote_id, access_token))
        row = next(row for row in self.remote if row["remote_id"] == remote_id)
        row["access_token"] = access_token
        return {"ok": True, "remote_id": remote_id}

    def liveness(self, local_id):
        self.liveness_calls.append(local_id)
        local = next(row for row in self.local if row["id"] == local_id)
        local["access_token"] = f"local-new-secret-{local_id}"
        return {"ok": True, "access_token": local["access_token"]}

    def service(self, **overrides):
        values = {
            "list_remote_fn": lambda: list(self.remote),
            "snapshot_fn": self.snapshot,
            "patch_fn": self.patch_remote,
            "list_local_fn": lambda: list(self.local),
            "liveness_fn": self.liveness,
            "audit_path": self.audit_path,
        }
        values.update(overrides)
        return ChatGPT2API401Service(**values)

    def wait_for(self, service, predicate, timeout=8.0):
        deadline = time.monotonic() + timeout
        state = service.state()
        while time.monotonic() < deadline:
            state = service.state()
            if predicate(state):
                return state
            time.sleep(0.01)
        self.fail(f"timed out waiting for state: {state}")

    def wait_terminal(self, service, expected=None, timeout=8.0):
        state = self.wait_for(
            service,
            lambda value: all(
                item["state"] not in {"queued", "refreshing", "ready"}
                for item in value["items"]
            ),
            timeout=timeout,
        )
        if expected is not None:
            self.assertEqual(expected, [item["state"] for item in state["items"]])
        return state

    def enqueue(self, service, remote_ids=None):
        return service.refresh(remote_ids or ["remote-old"], opt_in=True)

    def test_full_queue_flow_auto_replaces_only_locked_id_and_preserves_rt(self):
        service = self.service()
        detected = service.detect()
        self.assertEqual("pending", detected["items"][0]["state"])
        preserved = {key: self.remote[0][key] for key in ("email", "password", "refresh_token", "client_id", "last_refresh")}

        queued = self.enqueue(service)
        self.assertEqual(1, queued["queued_count"])
        self.assertEqual(["remote-old"], queued["submitted_ids"])
        self.assertEqual(CHATGPT2API_401_WORKERS, queued["workers"])
        final = self.wait_terminal(service, ["replaced"])

        self.assertEqual([7], self.liveness_calls)
        self.assertEqual([("remote-old", "local-new-secret-7")], self.patches)
        self.assertEqual("local-new-secret-7", self.remote[0]["access_token"])
        self.assertEqual(preserved, {key: self.remote[0][key] for key in preserved})
        item = final["items"][0]
        self.assertNotEqual(item["old_at_fingerprint"], item["new_at_fingerprint"])
        self.assertEqual(1, final["progress"]["completed"])
        self.assertEqual(0, final["progress"]["queued"])
        self.assertEqual(0, final["progress"]["active"])

    def test_enqueue_returns_before_blocked_liveness_completes(self):
        started = threading.Event()
        release = threading.Event()

        def blocked(local_id):
            started.set()
            release.wait(5)
            return self.liveness(local_id)

        service = self.service(liveness_fn=blocked)
        service.detect()
        before = time.monotonic()
        result = self.enqueue(service)
        elapsed = time.monotonic() - before
        self.assertEqual(1, result["queued_count"])
        self.assertLess(elapsed, 1.0)
        self.assertTrue(started.wait(2))
        self.assertIn(service.state()["items"][0]["state"], {"queued", "refreshing"})
        self.assertEqual([], self.patches)
        release.set()
        self.wait_terminal(service, ["replaced"])

    def test_fixed_five_workers_process_more_than_fifty_candidates(self):
        total = 60
        self.remote = [{
            "remote_id": f"remote-{index}",
            "email": f"user-{index}@example.com",
            "invalid": True,
            "reason": "HTTP 401",
            "access_token": f"remote-old-{index}",
            "refresh_token": f"refresh-{index}",
        } for index in range(total)]
        self.local = [{
            "id": index + 1,
            "email": f"user-{index}@example.com",
            "access_token": f"local-old-{index}",
        } for index in range(total)]
        lock = threading.Lock()
        release = threading.Event()
        reached_five = threading.Event()
        active = 0
        peak = 0

        def concurrent_liveness(local_id):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == CHATGPT2API_401_WORKERS:
                    reached_five.set()
            release.wait(5)
            try:
                return self.liveness(local_id)
            finally:
                with lock:
                    active -= 1

        service = self.service(liveness_fn=concurrent_liveness)
        detected = service.detect()
        ids = [item["remote_id"] for item in detected["items"]]
        self.assertEqual(total, len(ids))
        result = self.enqueue(service, ids)
        self.assertEqual(total, result["queued_count"])
        self.assertTrue(reached_five.wait(3))
        with lock:
            self.assertEqual(CHATGPT2API_401_WORKERS, peak)
            self.assertEqual(CHATGPT2API_401_WORKERS, active)
        release.set()
        final = self.wait_terminal(service, timeout=20)
        self.assertEqual(total, final["counts"]["replaced"])
        self.assertEqual(total, len(self.patches))
        with lock:
            self.assertLessEqual(peak, CHATGPT2API_401_WORKERS)

    def test_duplicate_enqueue_is_bounded_and_does_not_run_twice(self):
        started = threading.Event()
        release = threading.Event()

        def blocked(local_id):
            started.set()
            release.wait(5)
            return self.liveness(local_id)

        service = self.service(liveness_fn=blocked)
        service.detect()
        self.assertEqual(1, self.enqueue(service)["queued_count"])
        self.assertTrue(started.wait(2))
        duplicate = self.enqueue(service)
        self.assertEqual(0, duplicate["queued_count"])
        self.assertEqual(1, len(duplicate["skipped"]))
        self.assertLessEqual(len(duplicate["skipped"][0]["reason"]), 240)
        release.set()
        self.wait_terminal(service, ["replaced"])
        self.assertEqual([7], self.liveness_calls)

    def test_refresh_requires_literal_opt_in(self):
        service = self.service()
        service.detect()
        result = service.refresh(["remote-old"], opt_in=False)
        self.assertFalse(result["ok"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_detect_rejects_active_queue_without_destroying_it(self):
        started = threading.Event()
        release = threading.Event()

        def blocked(local_id):
            started.set()
            release.wait(5)
            return self.liveness(local_id)

        service = self.service(liveness_fn=blocked)
        service.detect()
        self.enqueue(service)
        self.assertTrue(started.wait(2))
        with self.assertRaises(ChatGPT2API401BusyError):
            service.detect()
        release.set()
        self.wait_terminal(service, ["replaced"])

    def test_second_service_instance_does_not_recover_live_in_process_job(self):
        started = threading.Event()
        release = threading.Event()

        def blocked(local_id):
            started.set()
            release.wait(5)
            return self.liveness(local_id)

        first = self.service(liveness_fn=blocked)
        first.detect()
        self.enqueue(first)
        self.assertTrue(started.wait(2))

        second = self.service(liveness_fn=blocked)
        active = second.state()["items"][0]
        self.assertIn(active["state"], {"queued", "refreshing"})
        self.assertNotEqual("restart", active["failure_stage"])
        with self.assertRaises(ChatGPT2API401BusyError):
            second.detect()

        release.set()
        final = self.wait_terminal(second, ["replaced"])
        self.assertEqual(1, final["progress"]["completed"])

    def test_executor_submit_failure_reverts_job_to_retryable_failed(self):
        class RejectedExecutor:
            @staticmethod
            def submit(*_args, **_kwargs):
                raise RuntimeError("executor unavailable access_token=must-not-leak")

        service = self.service()
        service.detect()
        service._executor = RejectedExecutor()
        result = self.enqueue(service)

        self.assertEqual(0, result["queued_count"])
        self.assertEqual(1, len(result["skipped"]))
        item = service.state()["items"][0]
        self.assertEqual("failed", item["state"])
        self.assertTrue(item["retryable"])
        self.assertEqual("restart", item["failure_stage"])
        serialized = json.dumps({"result": result, "state": service.state()})
        self.assertNotIn("must-not-leak", serialized)

    def test_duplicate_local_stable_id_is_ambiguous(self):
        self.local.append({
            "id": 7,
            "email": "other@example.com",
            "access_token": "other-local-secret",
        })
        service = self.service()
        detected = service.detect()
        self.assertEqual("ambiguous", detected["items"][0]["state"])
        self.assertIn("稳定 ID 重复", detected["items"][0]["reason"])
        self.assertEqual(0, self.enqueue(service)["queued_count"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_local_stable_id_drift_before_worker_blocks_liveness(self):
        service = self.service()
        service.detect()
        self.local.append({
            "id": 7,
            "email": "other@example.com",
            "access_token": "other-local-secret",
        })
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        self.assertEqual("pre_liveness", final["items"][0]["failure_stage"])
        self.assertIn("本地账号映射", final["items"][0]["reason"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_snapshot_drift_before_worker_blocks_liveness(self):
        service = self.service()
        service.detect()
        self.remote[0]["access_token"] = "changed-by-other-worker"
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        self.assertEqual("pre_liveness", final["items"][0]["failure_stage"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_rt_drift_before_worker_blocks_liveness(self):
        service = self.service()
        service.detect()
        self.remote[0]["refresh_token"] = "changed-refresh-token"
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        self.assertIn("保留字段", final["items"][0]["reason"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_liveness_failure_is_retryable_and_never_patches(self):
        service = self.service(liveness_fn=lambda _local_id: {"ok": False, "error": "upstream failed"})
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["failed"])
        item = final["items"][0]
        self.assertTrue(item["retryable"])
        self.assertEqual("liveness", item["failure_stage"])
        self.assertEqual([], self.patches)

    def delete_remote(self, remote_id):
        self.deletes.append(remote_id)
        self.remote[:] = [row for row in self.remote if row["remote_id"] != remote_id]
        return {"ok": True, "remote_id": remote_id}

    def deactivated_liveness(self, local_id):
        self.liveness_calls.append(local_id)
        local = next(row for row in self.local if row["id"] == local_id)
        local["live_check_status"] = "deactivated"
        return {"ok": False, "status": "deactivated", "error": "account_deactivated"}

    def test_deactivated_liveness_deletes_exact_locked_remote_id(self):
        self.deletes = []
        self.remote.append({
            "remote_id": "remote-healthy",
            "email": "two@example.com",
            "invalid": False,
            "access_token": "healthy-secret",
            "refresh_token": "healthy-refresh",
        })
        service = self.service(liveness_fn=self.deactivated_liveness, delete_fn=self.delete_remote)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["deleted"])
        item = final["items"][0]
        self.assertEqual([7], self.liveness_calls)
        self.assertEqual(["remote-old"], self.deletes)
        self.assertEqual([], self.patches)
        self.assertEqual(["remote-healthy"], [row["remote_id"] for row in self.remote])
        self.assertFalse(item["retryable"])
        self.assertTrue(item["deleted_at"])
        self.assertIn("account_deactivated", item["reason"])
        self.assertEqual(1, final["progress"]["completed"])
        self.assertEqual(0, final["progress"]["failed"])

    def test_local_deactivated_account_deletes_without_new_liveness(self):
        self.deletes = []
        self.local[0]["live_check_status"] = "deactivated"
        service = self.service(delete_fn=self.delete_remote)
        service.detect()
        self.enqueue(service)
        self.wait_terminal(service, ["deleted"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual(["remote-old"], self.deletes)
        self.assertEqual([], self.remote)

    def test_deactivated_delete_blocked_when_remote_at_changed(self):
        self.deletes = []

        def rotate_during_liveness(local_id):
            self.remote[0]["access_token"] = "rotated-elsewhere"
            return self.deactivated_liveness(local_id)

        service = self.service(liveness_fn=rotate_during_liveness, delete_fn=self.delete_remote)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        self.assertEqual("pre_delete", final["items"][0]["failure_stage"])
        self.assertEqual([], self.deletes)
        self.assertEqual(1, len(self.remote))

    def test_delete_failure_is_retryable_and_never_claims_deleted(self):
        self.deletes = []

        def broken_delete(remote_id):
            self.deletes.append(remote_id)
            raise RuntimeError("ChatGPT2API 管理接口暂时不可用")

        service = self.service(liveness_fn=self.deactivated_liveness, delete_fn=broken_delete)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["failed"])
        item = final["items"][0]
        self.assertEqual("delete", item["failure_stage"])
        self.assertTrue(item["retryable"])
        self.assertEqual(["remote-old"], self.deletes)
        self.assertEqual(1, len(self.remote))

    def test_delete_acknowledged_but_remote_row_remains_is_retryable_failed(self):
        self.deletes = []

        def ack_only(remote_id):
            self.deletes.append(remote_id)
            return {"ok": True, "remote_id": remote_id}

        service = self.service(liveness_fn=self.deactivated_liveness, delete_fn=ack_only)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["failed"])
        self.assertEqual("delete", final["items"][0]["failure_stage"])
        self.assertTrue(final["items"][0]["retryable"])

    def test_non_deactivated_liveness_failure_never_deletes(self):
        self.deletes = []
        service = self.service(
            liveness_fn=lambda _local_id: {"ok": False, "status": "failed", "error": "ProxyError: timeout"},
            delete_fn=self.delete_remote,
        )
        service.detect()
        self.enqueue(service)
        self.wait_terminal(service, ["failed"])
        self.assertEqual([], self.deletes)
        self.assertEqual(1, len(self.remote))

    def test_liveness_returned_token_must_equal_persisted_new_token(self):
        def mismatch(_local_id):
            self.local[0]["access_token"] = "persisted-token"
            return {"ok": True, "access_token": "returned-token"}

        service = self.service(liveness_fn=mismatch)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["failed"])
        self.assertIn("不一致", final["items"][0]["reason"])
        self.assertEqual([], self.patches)

    def test_unchanged_local_token_is_rejected(self):
        def unchanged(_local_id):
            return {"ok": True, "access_token": "local-old-secret"}

        service = self.service(liveness_fn=unchanged)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["failed"])
        self.assertIn("新的本地 AT", final["items"][0]["reason"])
        self.assertEqual([], self.patches)

    def test_mapping_drift_after_liveness_blocks_writeback(self):
        def drift(local_id):
            result = self.liveness(local_id)
            self.remote[0]["email"] = "changed@example.com"
            return result

        service = self.service(liveness_fn=drift)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        self.assertEqual("pre_writeback", final["items"][0]["failure_stage"])
        self.assertIn("映射", final["items"][0]["reason"])
        self.assertEqual([], self.patches)

    def test_old_at_drift_after_liveness_blocks_writeback(self):
        def drift(local_id):
            result = self.liveness(local_id)
            self.remote[0]["access_token"] = "changed-after-liveness"
            return result

        service = self.service(liveness_fn=drift)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        self.assertEqual("pre_writeback", final["items"][0]["failure_stage"])
        self.assertIn("旧 AT", final["items"][0]["reason"])
        self.assertEqual([], self.patches)

    def test_rt_drift_after_liveness_blocks_writeback(self):
        def drift(local_id):
            result = self.liveness(local_id)
            self.remote[0]["refresh_token"] = "changed-after-liveness"
            return result

        service = self.service(liveness_fn=drift)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        self.assertEqual("pre_writeback", final["items"][0]["failure_stage"])
        self.assertIn("保留字段", final["items"][0]["reason"])
        self.assertEqual([], self.patches)

    def test_patch_failure_is_conflict_and_never_claims_success(self):
        def rejected_patch(_remote_id, _access_token):
            raise RuntimeError("updated_ids omitted locked id")

        service = self.service(patch_fn=rejected_patch)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        item = final["items"][0]
        self.assertEqual("writeback", item["failure_stage"])
        self.assertEqual(0, final["progress"]["completed"])

    def test_patch_post_verification_rejects_rt_change(self):
        def bad_patch(remote_id, access_token):
            self.patch_remote(remote_id, access_token)
            self.remote[0]["refresh_token"] = "mutated-refresh-token"

        service = self.service(patch_fn=bad_patch)
        service.detect()
        self.enqueue(service)
        final = self.wait_terminal(service, ["conflict"])
        self.assertEqual("writeback", final["items"][0]["failure_stage"])
        self.assertIn("保留字段", final["items"][0]["reason"])

    def test_queued_and_refreshing_states_recover_as_retryable_failed(self):
        service = self.service()
        service.detect()
        payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
        original = payload["items"]["remote-old"]
        second = {**original, "remote_id": "remote-two", "state": "refreshing", "retryable": False}
        payload["items"]["remote-old"].update({"state": "queued", "retryable": False})
        payload["items"]["remote-two"] = second
        self.audit_path.write_text(json.dumps(payload), encoding="utf-8")

        recovered = self.service().state()
        self.assertEqual(["failed", "failed"], [item["state"] for item in recovered["items"]])
        self.assertTrue(all(item["retryable"] for item in recovered["items"]))
        self.assertTrue(all(item["failure_stage"] == "restart" for item in recovered["items"]))
        on_disk = json.loads(self.audit_path.read_text(encoding="utf-8"))
        self.assertTrue(all(item["state"] == "failed" for item in on_disk["items"].values()))

    def test_historical_ready_state_survives_restart_and_manual_replace(self):
        service = self.service()
        service.detect()
        self.local[0]["access_token"] = "historical-new-token"
        payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
        payload["items"]["remote-old"].update({
            "state": "ready",
            "new_at_sha256": hashlib.sha256(b"historical-new-token").hexdigest(),
            "retryable": False,
            "replaceable": True,
        })
        self.audit_path.write_text(json.dumps(payload), encoding="utf-8")

        reloaded = self.service()
        ready = reloaded.state()["items"][0]
        self.assertEqual("ready", ready["state"])
        self.assertTrue(ready["replaceable"])
        result = reloaded.replace(["remote-old"], opt_in=True)
        self.assertEqual(1, result["replaced_count"])
        self.assertEqual([("remote-old", "historical-new-token")], self.patches)

    def test_ambiguous_mapping_never_enqueues_or_mutates(self):
        self.remote.append({**self.remote[0], "remote_id": "remote-other", "access_token": "other-secret"})
        service = self.service()
        detected = service.detect()
        self.assertTrue(all(item["state"] == "ambiguous" for item in detected["items"]))
        result = self.enqueue(service)
        self.assertEqual(0, result["queued_count"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_unknown_selected_id_is_redacted_in_skipped_response(self):
        service = self.service()
        service.detect()
        result = service.refresh(
            ["access_token=top-secret callback=https://example.test/cb?code=secret"],
            opt_in=True,
        )
        serialized = json.dumps(result)
        self.assertIn("[redacted]", serialized)
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("https://example.test", serialized)

    def test_audit_is_atomic_private_symlink_safe_and_secret_free(self):
        target_dir = Path(self.temp_dir.name) / "data"
        target_dir.mkdir()
        target = target_dir / "audit.json"
        link = Path(self.temp_dir.name) / "audit-link.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unsupported")
        self.remote[0]["reason"] = "HTTP 401 access_token=remote-old-secret callback=https://example.test/cb?code=secret"
        service = self.service(audit_path=link)
        service.detect()
        self.assertTrue(link.is_symlink())
        raw = target.read_text(encoding="utf-8")
        for secret in (
            "remote-old-secret", "local-old-secret", "password-secret", "refresh-secret",
            "client-secret", "https://example.test", "code=secret",
        ):
            self.assertNotIn(secret, raw)
        parsed = json.loads(raw)
        self.assertEqual("pending", parsed["items"]["remote-old"]["state"])
        if os.name != "nt":
            self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertEqual([], list(target_dir.glob("*.tmp")))

    def test_corrupt_audit_counts_are_safely_bounded(self):
        self.audit_path.write_text(json.dumps({
            "remote_counts": {"total": "not-a-number", "invalid": -3, "healthy": 9_999_999},
            "items": {},
        }), encoding="utf-8")
        state = self.service().state()
        self.assertEqual(0, state["counts"]["total"])
        self.assertEqual(0, state["counts"]["invalid"])
        self.assertEqual(1_000_000, state["counts"]["healthy"])


if __name__ == "__main__":
    unittest.main()
