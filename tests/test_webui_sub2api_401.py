import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from core.sub2api_401_service import Sub2ApiSnapshotConflict
from core.sub2api_admin import Sub2ApiAdminError
from webui.app import create_app


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.target = kwargs.get("target")
        self.args = kwargs.get("args", ())

    def start(self):
        return None


class _FailingStartThread(_NoopThread):
    def start(self):
        raise RuntimeError("access_token=secret callback=https://secret.example/cb?code=secret")


class WebuiSub2Api401Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def assert_sensitive_headers(self, response):
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual("nosniff", response.headers.get("X-Content-Type-Options"))

    def test_page_and_all_routes_require_auth_and_old_routes_are_removed(self):
        self.assertEqual(302, self.client.get("/sub2api-401").status_code)
        for method, path in (
            ("get", "/api/sub2api-401/state"),
            ("post", "/api/sub2api-401/detect"),
            ("post", "/api/sub2api-401/retry"),
            ("post", "/api/sub2api-401/cleanup"),
        ):
            with self.subTest(path=path):
                self.assertEqual(401, getattr(self.client, method)(path).status_code)
        self.assertEqual(404, self.client.get("/cpa-401", headers=self.headers).status_code)
        self.assertEqual(404, self.client.get("/api/cpa-401/state", headers=self.headers).status_code)

    def test_state_and_detect_return_non_secret_account_id_json_with_headers(self):
        payload = {
            "ok": True,
            "supported": True,
            "cleanup_supported": True,
            "items": [{
                "account_id": "old-id", "account_name": "bad", "email": "bad@example.com",
                "state": "pending", "health": "unauthorized", "reason": "Sub2API 返回 401",
                "observed_account_ids": ["old-id"], "pre_retry_account_ids": [],
                "post_retry_account_ids": [], "replacement_account_id": "", "cleanup_account_id": "",
                "candidate_count": 1, "retryable": True, "checked_at": "2026-10-12T00:00:00+00:00",
                "local_codex_status": "failed",
            }],
            "counts": {"unauthorized": 1, "healthy": 0, "timeout": 0, "unknown": 0, "verified": 0},
            "checked_at": "2026-10-12T00:00:00+00:00",
        }
        with patch("core.sub2api_401_service.Sub2Api401Service.state", return_value=payload):
            state_response = self.client.get("/api/sub2api-401/state", headers=self.headers)
        with patch("webui.app.db.list_accounts", return_value=[]), patch(
            "core.sub2api_401_service.Sub2Api401Service.detect", return_value=payload
        ):
            detect_response = self.client.post("/api/sub2api-401/detect", headers=self.headers)
        for response in (state_response, detect_response):
            self.assertEqual(200, response.status_code)
            self.assert_sensitive_headers(response)
            self.assertEqual("old-id", response.get_json()["items"][0]["account_id"])
            text = response.get_data(as_text=True).lower()
            self.assertNotIn("access_token", text)
            self.assertNotIn("refresh_token", text)
            self.assertNotIn("callback", text)

    def test_detect_provider_errors_are_bounded_and_do_not_echo_secrets(self):
        cases = [
            (Sub2ApiAdminError("auth", status_code=502, category="auth"), 502, "认证失败"),
            (Sub2ApiAdminError("timeout", status_code=504, category="timeout"), 504, "超时"),
            (Sub2ApiAdminError("unsupported", status_code=501, category="unsupported"), 501, "不支持"),
        ]
        for error, expected_status, expected_text in cases:
            with self.subTest(status=expected_status), patch("webui.app.db.list_accounts", return_value=[]), patch(
                "core.sub2api_401_service.Sub2Api401Service.detect", side_effect=error
            ):
                response = self.client.post("/api/sub2api-401/detect", headers=self.headers)
            self.assertEqual(expected_status, response.status_code)
            self.assert_sensitive_headers(response)
            self.assertIn(expected_text, response.get_json()["error"])
            self.assertNotIn("access_token", response.get_data(as_text=True).lower())

    def test_retry_reuses_existing_worker_and_persists_account_id_snapshot(self):
        candidate = {"account_id": "old-id", "email": "bad@example.com", "retryable": True, "state": "pending"}
        with tempfile.TemporaryDirectory() as tmp, patch(
            "core.sub2api_401_service.Sub2Api401Service.select_retryable",
            return_value=([candidate], [{"account_id": "invalid", "reason": "不存在"}]),
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.mark_retrying"
        ) as mark_retrying, patch(
            "webui.app.db.get_account_by_email", return_value={"id": 1, "email": "bad@example.com"}
        ), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch(
            "webui.app.db.update_account_codex_status"
        ), patch(
            "webui.app.codex_retry_service.log_path", return_value=Path(tmp) / "retry.log"
        ), patch(
            "webui.app.threading.Thread", _NoopThread
        ):
            response = self.client.post(
                "/api/sub2api-401/retry",
                headers=self.headers,
                json={"account_ids": ["old-id", "old-id", "invalid"], "workers": 2},
            )
        self.assertEqual(200, response.status_code)
        self.assert_sensitive_headers(response)
        body = response.get_json()
        self.assertEqual(1, body["started_count"])
        self.assertIn("自动删除锁定旧 ID", body["message"])
        self.assertEqual("old-id", body["started"][0]["account_id"])
        mark_retrying.assert_called_once_with(["old-id"], batch_id=ANY)

    def test_retry_defaults_workers_to_three(self):
        candidate = {"account_id": "old-id", "email": "bad@example.com", "retryable": True, "state": "pending"}
        with patch(
            "core.sub2api_401_service.Sub2Api401Service.select_retryable",
            return_value=([candidate], []),
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.mark_retrying"
        ), patch(
            "webui.app.db.get_account_by_email", return_value={"id": 1, "email": "bad@example.com"}
        ), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch(
            "webui.app.db.update_account_codex_status"
        ), patch(
            "webui.app.codex_retry_service.log_path",
            return_value=Path(tempfile.gettempdir()) / "retry.log",
        ), patch(
            "webui.app.threading.Thread"
        ) as thread:
            response = self.client.post(
                "/api/sub2api-401/retry",
                headers=self.headers,
                json={"account_ids": ["old-id"]},
            )
        self.assertEqual(200, response.status_code)
        thread.assert_called_once()
        self.assertEqual(3, thread.call_args.kwargs["args"][1])

    def test_retry_runner_detects_after_workers_and_cleans_only_selected_ready_ids(self):
        candidates = [
            {"account_id": "old-a", "email": "a@example.com", "retryable": True, "state": "pending"},
            {"account_id": "old-b", "email": "b@example.com", "retryable": True, "state": "pending"},
        ]
        events = []
        batch_ids = []

        def mark_retrying(_account_ids, *, batch_id):
            batch_ids.append(batch_id)

        def run_worker(email, **_kwargs):
            events.append(f"worker:{email}")

        def detect(_local_status):
            events.append("detect")
            return {
                "items": [
                    {"account_id": "old-a", "state": "cleanup_ready", "retry_batch_id": batch_ids[0]},
                    {"account_id": "old-b", "state": "cleanup_ready", "retry_batch_id": "older-batch"},
                    {"account_id": "unrelated", "state": "cleanup_ready", "retry_batch_id": batch_ids[0]},
                ]
            }

        def cleanup(_account_ids, **_kwargs):
            events.append("cleanup")
            return {
                "items": [{
                    "account_id": "old-a",
                    "cleanup_account_id": "old-a",
                    "replacement_account_id": "new-a",
                    "ok": False,
                    "result": "not_deleted",
                    "reason": "Sub2API 删除请求失败",
                }]
            }

        with tempfile.TemporaryDirectory() as tmp, patch(
            "core.sub2api_401_service.Sub2Api401Service.select_retryable",
            return_value=(candidates, []),
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.mark_retrying",
            side_effect=mark_retrying,
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.detect", side_effect=detect
        ) as detect_mock, patch(
            "core.sub2api_401_service.Sub2Api401Service.cleanup", side_effect=cleanup
        ) as cleanup_mock, patch(
            "webui.app.db.get_account_by_email",
            side_effect=lambda email: {"id": email, "email": email},
        ), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch(
            "webui.app.db.update_account_codex_status"
        ), patch(
            "webui.app.codex_retry_service.log_path",
            side_effect=lambda email: Path(tmp) / f"{email}.log",
        ), patch(
            "webui.app.db.list_accounts", return_value=[]
        ), patch(
            "webui.app.codex_retry_service.run_worker", side_effect=run_worker
        ):
            with patch("webui.app.threading.Thread") as thread:
                response = self.client.post(
                    "/api/sub2api-401/retry",
                    headers=self.headers,
                    json={"account_ids": ["old-a", "old-b"], "workers": 2},
                )
                runner = thread.call_args.kwargs["target"]
                runner_args = thread.call_args.kwargs["args"]
            runner(*runner_args)

        self.assertEqual(200, response.status_code)
        self.assert_sensitive_headers(response)
        worker_indexes = [index for index, event in enumerate(events) if event.startswith("worker:")]
        self.assertEqual(2, len(worker_indexes))
        self.assertGreater(events.index("detect"), max(worker_indexes))
        self.assertGreater(events.index("cleanup"), events.index("detect"))
        detect_mock.assert_called_once_with({})
        cleanup_mock.assert_called_once_with(["old-a"], opt_in=True, limit=50)

    def test_retry_rejects_non_integer_workers(self):
        for workers in (True, False, "3", 3.5, None):
            with self.subTest(workers=workers):
                response = self.client.post(
                    "/api/sub2api-401/retry",
                    headers=self.headers,
                    json={"account_ids": ["old-id"], "workers": workers},
                )
                self.assertEqual(400, response.status_code)
                self.assert_sensitive_headers(response)
                self.assertEqual("workers 必须是整数", response.get_json()["error"])

    def test_retry_snapshot_conflict_releases_reservation_and_returns_409(self):
        candidate = {"account_id": "old-id", "email": "bad@example.com", "retryable": True, "state": "pending"}
        with patch(
            "core.sub2api_401_service.Sub2Api401Service.select_retryable", return_value=([candidate], [])
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.mark_retrying",
            side_effect=Sub2ApiSnapshotConflict(["old-id"]),
        ), patch(
            "webui.app.db.get_account_by_email", return_value={"id": 1, "email": "bad@example.com"}
        ), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch(
            "webui.app.codex_retry_service.release"
        ) as release:
            response = self.client.post(
                "/api/sub2api-401/retry", headers=self.headers, json={"account_ids": ["old-id"]}
            )
        self.assertEqual(409, response.status_code)
        self.assert_sensitive_headers(response)
        self.assertEqual(["old-id"], response.get_json()["conflicts"])
        release.assert_called_once_with("bad@example.com")

    def test_retry_thread_start_failure_releases_reservation_and_returns_safe_error(self):
        candidate = {"account_id": "old-id", "email": "bad@example.com", "retryable": True, "state": "pending"}
        with tempfile.TemporaryDirectory() as tmp, patch(
            "core.sub2api_401_service.Sub2Api401Service.select_retryable", return_value=([candidate], [])
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.mark_retrying"
        ), patch(
            "core.sub2api_401_service.Sub2Api401Service.mark_verification_error"
        ) as mark_error, patch(
            "webui.app.db.get_account_by_email", return_value={"id": 1, "email": "bad@example.com"}
        ), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch(
            "webui.app.codex_retry_service.release"
        ) as release, patch(
            "webui.app.db.update_account_codex_status"
        ), patch(
            "webui.app.codex_retry_service.log_path", return_value=Path(tmp) / "retry.log"
        ), patch(
            "webui.app.threading.Thread", _FailingStartThread
        ):
            response = self.client.post(
                "/api/sub2api-401/retry", headers=self.headers, json={"account_ids": ["old-id"]}
            )
        self.assertEqual(500, response.status_code)
        self.assert_sensitive_headers(response)
        self.assertNotIn("secret", response.get_data(as_text=True).lower())
        release.assert_called_once_with("bad@example.com")
        mark_error.assert_called_once_with(["old-id"], "Sub2API 401 补跑调度失败；旧账号保持不变")

    def test_retry_all_skipped_is_422_with_sensitive_headers(self):
        with patch(
            "core.sub2api_401_service.Sub2Api401Service.select_retryable",
            return_value=([], [{"account_id": "missing", "reason": "不在最近一次检测结果中"}]),
        ):
            response = self.client.post(
                "/api/sub2api-401/retry", headers=self.headers, json={"account_ids": ["missing"]}
            )
        self.assertEqual(422, response.status_code)
        self.assert_sensitive_headers(response)
        self.assertEqual(0, response.get_json()["started_count"])

    def test_cleanup_errors_are_bounded_and_do_not_echo_secrets(self):
        with patch(
            "core.sub2api_401_service.Sub2Api401Service.cleanup",
            side_effect=RuntimeError(
                "access_token=super-secret callback=https://secret.example/cb?code=secret-code"
            ),
        ):
            response = self.client.post(
                "/api/sub2api-401/cleanup",
                headers=self.headers,
                json={"account_ids": ["old-id"], "confirm_cleanup": True},
            )
        self.assertEqual(502, response.status_code)
        self.assert_sensitive_headers(response)
        text = response.get_data(as_text=True).lower()
        self.assertNotIn("super-secret", text)
        self.assertNotIn("secret.example", text)
        self.assertNotIn("secret-code", text)

    def test_cleanup_passes_explicit_confirmation_and_account_ids(self):
        payload = {
            "ok": True,
            "cleanup_supported": True,
            "cleanup_performed": False,
            "manual_action": "确认后删除",
            "items": [{
                "account_id": "old-id",
                "email": "bad@example.com",
                "replacement_account_id": "new-id",
                "cleanup_account_id": "old-id",
            }],
        }
        with patch("core.sub2api_401_service.Sub2Api401Service.cleanup", return_value=payload) as cleanup:
            response = self.client.post(
                "/api/sub2api-401/cleanup",
                headers=self.headers,
                json={"account_ids": ["old-id"], "confirm_cleanup": True},
            )
        self.assertEqual(200, response.status_code)
        self.assert_sensitive_headers(response)
        cleanup.assert_called_once_with(["old-id"], opt_in=True, limit=50)

    def test_batch_request_validation_is_bounded(self):
        cases = [
            ("/api/sub2api-401/retry", None),
            ("/api/sub2api-401/retry", {"account_ids": []}),
            ("/api/sub2api-401/retry", {"account_ids": ["a"] * 51}),
            ("/api/sub2api-401/cleanup", None),
            ("/api/sub2api-401/cleanup", {"account_ids": []}),
            ("/api/sub2api-401/cleanup", {"account_ids": ["a"] * 51}),
        ]
        for path, payload in cases:
            with self.subTest(path=path, payload_type=type(payload).__name__):
                kwargs = {"headers": self.headers}
                if payload is None:
                    kwargs["data"] = "[]"
                    kwargs["content_type"] = "application/json"
                else:
                    kwargs["json"] = payload
                response = self.client.post(path, **kwargs)
                self.assertEqual(400, response.status_code)
                self.assert_sensitive_headers(response)


if __name__ == "__main__":
    unittest.main()
