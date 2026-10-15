import os
import tempfile
import unittest
from unittest.mock import ANY, patch

from core.chatgpt2api_401_service import ChatGPT2API401BusyError
from webui.app import create_app


class WebUICredentialHealthSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {"CREDENTIAL_HEALTH_SCHEDULER_PATH": os.path.join(self.tmp.name, "scheduler.json")},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = create_app(auth_code="test-auth")
        self.addCleanup(self.app.extensions["credential_health_scheduler"].stop)
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def assert_private(self, response):
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual("nosniff", response.headers.get("X-Content-Type-Options"))

    def test_scheduler_routes_require_authentication(self):
        for method, path in (
            ("get", "/api/sub2api-401/scheduler"),
            ("put", "/api/sub2api-401/scheduler"),
            ("get", "/api/chatgpt2api-401/scheduler"),
            ("put", "/api/chatgpt2api-401/scheduler"),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(401, response.status_code)
                self.assert_private(response)
                self.assertEqual("no-cache", response.headers.get("Pragma"))

    def test_independent_scheduler_state_and_configuration(self):
        states = {
            "sub2api_401": {"enabled": True, "interval_minutes": 30, "phase": "idle"},
            "chatgpt2api_401": {"enabled": False, "interval_minutes": 60, "phase": "disabled"},
        }
        scheduler = self.app.extensions["credential_health_scheduler"]
        with patch.object(scheduler, "state", side_effect=lambda name: dict(states[name])):
            sub = self.client.get("/api/sub2api-401/scheduler", headers=self.headers)
            chat = self.client.get("/api/chatgpt2api-401/scheduler", headers=self.headers)
        self.assertEqual(30, sub.get_json()["scheduler"]["interval_minutes"])
        self.assertEqual(60, chat.get_json()["scheduler"]["interval_minutes"])
        self.assert_private(sub)
        self.assert_private(chat)

        with patch.object(scheduler, "configure", return_value=states["sub2api_401"]) as configure:
            response = self.client.put(
                "/api/sub2api-401/scheduler",
                headers=self.headers,
                json={"enabled": True, "interval_minutes": 30},
            )
        self.assertEqual(200, response.status_code)
        configure.assert_called_once_with("sub2api_401", enabled=True, interval_minutes=30)
        self.assert_private(response)

        with patch.object(scheduler, "configure", return_value=states["chatgpt2api_401"]) as configure:
            response = self.client.put(
                "/api/chatgpt2api-401/scheduler",
                headers=self.headers,
                json={"enabled": False, "interval_minutes": 60},
            )
        self.assertEqual(200, response.status_code)
        configure.assert_called_once_with("chatgpt2api_401", enabled=False, interval_minutes=60)

    def test_scheduler_configuration_persists_across_new_app_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = os.path.join(temp_dir, "credential-health.json")
            with patch.dict(os.environ, {"CREDENTIAL_HEALTH_SCHEDULER_PATH": state_path}):
                first = create_app(auth_code="persist-auth")
                response = first.test_client().put(
                    "/api/sub2api-401/scheduler",
                    headers={"X-Auth-Code": "persist-auth"},
                    json={"enabled": False, "interval_minutes": 60},
                )
                self.assertEqual(200, response.status_code)
                first.extensions["credential_health_scheduler"].stop()

                second = create_app(auth_code="persist-auth")
                restored = second.test_client().get(
                    "/api/sub2api-401/scheduler",
                    headers={"X-Auth-Code": "persist-auth"},
                )
                self.assertEqual(60, restored.get_json()["scheduler"]["interval_minutes"])
                self.assertFalse(restored.get_json()["scheduler"]["enabled"])
                second.extensions["credential_health_scheduler"].stop()

    def test_scheduler_configuration_validation_is_bounded(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        for path in ("/api/sub2api-401/scheduler", "/api/chatgpt2api-401/scheduler"):
            with self.subTest(path=path):
                response = self.client.put(path, headers=self.headers, data="[]", content_type="application/json")
                self.assertEqual(400, response.status_code)
                self.assert_private(response)
                with patch.object(scheduler, "configure", side_effect=ValueError("interval_minutes 必须是 5–1440 的整数")):
                    response = self.client.put(
                        path,
                        headers=self.headers,
                        json={"enabled": True, "interval_minutes": 4},
                    )
                self.assertEqual(400, response.status_code)
                self.assertNotIn("access_token", response.get_data(as_text=True).lower())

    def test_state_responses_include_scheduler_projection(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        with patch("core.sub2api_401_service.Sub2Api401Service.state", return_value={"ok": True, "items": []}), patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.state", return_value={"ok": True, "items": [], "progress": {}}
        ), patch.object(
            scheduler,
            "state",
            side_effect=lambda name: {"enabled": name == "sub2api_401", "interval_minutes": 30, "phase": "idle"},
        ):
            sub = self.client.get("/api/sub2api-401/state", headers=self.headers)
            chat = self.client.get("/api/chatgpt2api-401/state", headers=self.headers)
        self.assertTrue(sub.get_json()["scheduler"]["enabled"])
        self.assertFalse(chat.get_json()["scheduler"]["enabled"])

    def test_sub2api_automatic_job_skips_while_manual_retry_is_active(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        current = {
            "ok": True,
            "items": [{"account_id": "old-exact-id", "email": "user@example.com", "state": "retrying"}],
        }
        with patch(
            "core.sub2api_401_service.Sub2Api401Service.state", return_value=current
        ), patch("webui.app.codex_retry_service.is_retrying", return_value=True), patch(
            "core.sub2api_401_service.Sub2Api401Service.detect"
        ) as detect:
            result = scheduler._jobs["sub2api_401"]("manual-busy-run")
        self.assertEqual("skipped_busy", result.outcome)
        self.assertEqual(1, result.skipped)
        detect.assert_not_called()

    def test_chatgpt2api_automatic_job_skips_while_manual_queue_is_active(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        with patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.detect",
            side_effect=ChatGPT2API401BusyError("queue active"),
        ), patch("core.chatgpt2api_401_service.ChatGPT2API401Service.refresh") as refresh:
            result = scheduler._jobs["chatgpt2api_401"]("manual-busy-run")
        self.assertEqual("skipped_busy", result.outcome)
        self.assertEqual(1, result.skipped)
        refresh.assert_not_called()

    def test_sub2api_automatic_job_reuses_retry_and_exact_cleanup(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        candidate = {
            "account_id": "old-exact-id",
            "email": "user@example.com",
            "state": "pending",
            "health": "unauthorized",
            "retryable": True,
        }
        fresh = {"ok": True, "items": [candidate]}
        batch = {}

        def mark_selected(_ids, *, batch_id=""):
            batch["id"] = batch_id

        def detect_state(*_args, **_kwargs):
            if "id" not in batch:
                return fresh
            return {"ok": True, "items": [{**candidate, "state": "cleanup_ready", "retry_batch_id": batch["id"]}]}

        with patch("core.sub2api_401_service.Sub2Api401Service.state", return_value={"items": []}), patch(
            "core.sub2api_401_service.Sub2Api401Service.detect", side_effect=detect_state
        ) as detect, patch(
            "core.sub2api_401_service.Sub2Api401Service.select_retryable", return_value=([candidate], [])
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.mark_retrying", side_effect=mark_selected
        ) as mark_retrying, patch(
            "core.sub2api_401_service.Sub2Api401Service.cleanup", return_value={"items": [{"ok": True}]}
        ) as cleanup, patch(
            "webui.app.db.list_accounts", return_value=[]
        ), patch(
            "webui.app.db.get_account_by_email", return_value={"id": 7, "email": "user@example.com"}
        ), patch(
            "webui.app.db.update_account_codex_status"
        ), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch(
            "webui.app.codex_retry_service.run_worker"
        ):
            result = scheduler._jobs["sub2api_401"]("scheduled-run")
        self.assertEqual("success", result.outcome)
        self.assertEqual(1, result.completed)
        self.assertEqual(2, detect.call_count)
        mark_retrying.assert_called_once_with(["old-exact-id"], batch_id=ANY)
        cleanup.assert_called_once_with(["old-exact-id"], opt_in=True, limit=50)

    def test_sub2api_automatic_job_deletes_deactivated_rows(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        fresh = {"ok": True, "items": [{"account_id": "dead-id", "email": "dead@example.com", "state": "deactivated"}]}
        with patch("core.sub2api_401_service.Sub2Api401Service.state", return_value={"items": []}), patch(
            "core.sub2api_401_service.Sub2Api401Service.detect", return_value=fresh
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.delete_deactivated",
            return_value={"items": [{"account_id": "dead-id", "ok": True}], "deleted_count": 1},
        ) as delete_deactivated, patch("webui.app.db.list_accounts", return_value=[]):
            result = scheduler._jobs["sub2api_401"]("dead-run")
        delete_deactivated.assert_called_once_with(["dead-id"], opt_in=True, limit=50)
        self.assertEqual("success", result.outcome)
        self.assertEqual(1, result.completed)
        self.assertEqual(0, result.failed)

    def test_sub2api_automatic_job_counts_unverified_retry_as_failed(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        candidate = {
            "account_id": "old-exact-id",
            "email": "user@example.com",
            "state": "pending",
            "health": "unauthorized",
            "retryable": True,
        }
        batch = {}

        def mark_selected(_ids, *, batch_id=""):
            batch["id"] = batch_id

        def detect_state(*_args, **_kwargs):
            if "id" not in batch:
                return {"ok": True, "items": [candidate]}
            return {
                "ok": True,
                "items": [{**candidate, "state": "ambiguous", "retry_batch_id": batch["id"]}],
            }

        with patch("core.sub2api_401_service.Sub2Api401Service.state", return_value={"items": []}), patch(
            "core.sub2api_401_service.Sub2Api401Service.detect", side_effect=detect_state
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.select_retryable", return_value=([candidate], [])
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.mark_retrying", side_effect=mark_selected
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.cleanup"
        ) as cleanup, patch(
            "webui.app.db.list_accounts", return_value=[]
        ), patch(
            "webui.app.db.get_account_by_email", return_value={"id": 7, "email": "user@example.com"}
        ), patch(
            "webui.app.db.update_account_codex_status"
        ), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch(
            "webui.app.codex_retry_service.run_worker"
        ):
            result = scheduler._jobs["sub2api_401"]("ambiguous-run")
        self.assertEqual("partial", result.outcome)
        self.assertEqual(0, result.completed)
        self.assertEqual(1, result.failed)
        cleanup.assert_not_called()

    def test_sub2api_restart_reconciles_stale_retrying_instead_of_blocking_forever(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        current = {"items": [{"account_id": "old-id", "email": "user@example.com", "state": "retrying"}]}
        with patch("core.sub2api_401_service.Sub2Api401Service.state", return_value=current), patch(
            "webui.app.db.list_accounts", return_value=[{"email": "user@example.com", "codex_status": "retrying"}]
        ), patch(
            "webui.app.codex_retry_service.is_retrying", return_value=False
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.detect", return_value={"ok": True, "items": []}
        ) as detect:
            result = scheduler._jobs["sub2api_401"]("restart-run")
        self.assertEqual("success", result.outcome)
        detect.assert_called_once_with({"user@example.com": "failed"})

    def test_chatgpt2api_automatic_job_reuses_queue_and_exact_writeback(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        fresh = {
            "ok": True,
            "items": [{"remote_id": "remote-exact-id", "state": "pending", "retryable": True}],
        }
        terminal = {
            "ok": True,
            "items": [{"remote_id": "remote-exact-id", "state": "replaced"}],
            "progress": {"queued": 0, "active": 0},
        }
        with patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.detect", return_value=fresh
        ), patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.refresh",
            return_value={"queued_count": 1, "submitted_ids": ["remote-exact-id"], "skipped": []},
        ) as refresh, patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.state", return_value=terminal
        ):
            result = scheduler._jobs["chatgpt2api_401"]("scheduled-run")
        self.assertEqual("success", result.outcome)
        self.assertEqual(1, result.completed)
        refresh.assert_called_once_with(["remote-exact-id"], opt_in=True, limit=5000)

    def test_chatgpt2api_automatic_job_counts_deleted_deactivated_as_completed(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        fresh = {
            "ok": True,
            "items": [
                {"remote_id": "replaced-id", "state": "pending", "retryable": True},
                {"remote_id": "deleted-id", "state": "pending", "retryable": True},
            ],
        }
        terminal = {
            "ok": True,
            "items": [
                {"remote_id": "replaced-id", "state": "replaced"},
                {"remote_id": "deleted-id", "state": "deleted"},
            ],
            "progress": {"queued": 0, "active": 0},
        }
        with patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.detect", return_value=fresh
        ), patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.refresh",
            return_value={"queued_count": 2, "submitted_ids": ["replaced-id", "deleted-id"], "skipped": []},
        ), patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.state", return_value=terminal
        ):
            result = scheduler._jobs["chatgpt2api_401"]("deleted-run")
        self.assertEqual("success", result.outcome)
        self.assertEqual(2, result.completed)
        self.assertEqual(0, result.failed)

    def test_chatgpt2api_automatic_job_observes_only_ids_accepted_by_this_round(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        fresh = {
            "ok": True,
            "items": [
                {"remote_id": "accepted-id", "state": "pending", "retryable": True},
                {"remote_id": "skipped-id", "state": "pending", "retryable": True},
            ],
        }
        terminal = {
            "ok": True,
            "items": [
                {"remote_id": "accepted-id", "state": "replaced"},
                {"remote_id": "skipped-id", "state": "replaced"},
            ],
            "progress": {"queued": 0, "active": 0},
        }
        with patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.detect", return_value=fresh
        ), patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.refresh",
            return_value={
                "queued_count": 1,
                "submitted_ids": ["accepted-id"],
                "skipped": [{"remote_id": "skipped-id", "reason": "队列已满"}],
            },
        ), patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.state", return_value=terminal
        ):
            result = scheduler._jobs["chatgpt2api_401"]("mixed-run")
        self.assertEqual("partial", result.outcome)
        self.assertEqual(1, result.submitted)
        self.assertEqual(1, result.completed)
        self.assertEqual(1, result.skipped)

    def test_chatgpt2api_missing_queued_audit_item_is_failure_not_success(self):
        scheduler = self.app.extensions["credential_health_scheduler"]
        fresh = {
            "ok": True,
            "items": [{"remote_id": "remote-exact-id", "state": "pending", "retryable": True}],
        }
        with patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.detect", return_value=fresh
        ), patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.refresh",
            return_value={"queued_count": 1, "submitted_ids": ["remote-exact-id"], "skipped": []},
        ), patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.state",
            return_value={"ok": True, "items": [], "progress": {"queued": 0, "active": 0}},
        ):
            result = scheduler._jobs["chatgpt2api_401"]("missing-run")
        self.assertEqual("partial", result.outcome)
        self.assertEqual(0, result.completed)
        self.assertEqual(1, result.failed)


if __name__ == "__main__":
    unittest.main()
