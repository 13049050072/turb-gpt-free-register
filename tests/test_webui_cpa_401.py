import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from core.cpa_401_service import CpaSnapshotConflict
from webui.app import create_app


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.target = kwargs.get("target")
        self.args = kwargs.get("args", ())

    def start(self):
        return None


class WebuiCpa401Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def assert_sensitive_headers(self, response):
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual("nosniff", response.headers.get("X-Content-Type-Options"))

    def test_page_and_all_routes_require_auth(self):
        self.assertEqual(302, self.client.get("/cpa-401").status_code)
        for method, path in (
            ("get", "/api/cpa-401/state"),
            ("post", "/api/cpa-401/detect"),
            ("post", "/api/cpa-401/retry"),
            ("post", "/api/cpa-401/cleanup"),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(401, response.status_code)

    def test_state_and_detect_return_bounded_non_secret_json_with_headers(self):
        payload = {
            "ok": True,
            "supported": True,
            "cleanup_supported": False,
            "items": [{
                "email": "bad@example.com", "state": "pending", "reason": "CPA 返回 401",
                "files": ["codex-bad.json"], "pre_retry_files": [], "post_retry_files": [],
                "cleanup_files": [], "replacement_file": "", "candidate_count": 1,
                "retryable": True, "checked_at": "2026-10-12T00:00:00+00:00",
                "local_codex_status": "failed",
            }],
            "counts": {"unauthorized": 1, "healthy": 0, "unknown": 0, "verified": 0},
            "checked_at": "2026-10-12T00:00:00+00:00",
        }
        with patch("core.cpa_401_service.Cpa401Service.state", return_value=payload):
            state_response = self.client.get("/api/cpa-401/state", headers=self.headers)
        with patch("webui.app.db.list_accounts", return_value=[]), patch(
            "core.cpa_401_service.Cpa401Service.detect", return_value=payload
        ):
            detect_response = self.client.post("/api/cpa-401/detect", headers=self.headers)
        for response in (state_response, detect_response):
            self.assertEqual(200, response.status_code)
            self.assert_sensitive_headers(response)
            self.assertEqual("bad@example.com", response.get_json()["items"][0]["email"])
            text = response.get_data(as_text=True).lower()
            self.assertNotIn("access_token", text)
            self.assertNotIn("refresh_token", text)

    def test_detect_provider_errors_are_bounded_and_sensitive_headers_remain(self):
        cases = [
            (RuntimeError("status=401 access_token=super-secret callback=https://secret"), 502, "认证失败"),
            (RuntimeError("request timeout access_token=secret"), 504, "超时"),
            (RuntimeError("status=404 not_found"), 501, "不支持"),
        ]
        for error, expected_status, expected_text in cases:
            with self.subTest(status=expected_status), patch("webui.app.db.list_accounts", return_value=[]), patch(
                "core.cpa_401_service.Cpa401Service.detect", side_effect=error
            ):
                response = self.client.post("/api/cpa-401/detect", headers=self.headers)
            self.assertEqual(expected_status, response.status_code)
            self.assert_sensitive_headers(response)
            text = response.get_data(as_text=True)
            self.assertIn(expected_text, response.get_json()["error"])
            self.assertNotIn("super-secret", text)
            self.assertNotIn("https://secret", text)

    def test_retry_reuses_existing_worker_and_persists_batch_snapshot(self):
        candidate = {"email": "bad@example.com", "retryable": True, "state": "pending"}
        with tempfile.TemporaryDirectory() as tmp, patch(
            "core.cpa_401_service.Cpa401Service.select_retryable",
            return_value=([candidate], [{"email": "invalid", "reason": "邮箱非法"}]),
        ), patch(
            "core.cpa_401_service.Cpa401Service.mark_retrying"
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
                "/api/cpa-401/retry",
                headers=self.headers,
                json={"emails": ["bad@example.com", "bad@example.com", "invalid"], "workers": 2},
            )
        self.assertEqual(200, response.status_code)
        self.assert_sensitive_headers(response)
        body = response.get_json()
        self.assertEqual(1, body["started_count"])
        self.assertEqual("bad@example.com", body["started"][0]["email"])
        mark_retrying.assert_called_once_with(["bad@example.com"], batch_id=ANY)

    def test_retry_snapshot_conflict_releases_reservation_and_returns_409(self):
        candidate = {"email": "bad@example.com", "retryable": True, "state": "pending"}
        with patch(
            "core.cpa_401_service.Cpa401Service.select_retryable",
            return_value=([candidate], []),
        ), patch(
            "core.cpa_401_service.Cpa401Service.mark_retrying",
            side_effect=CpaSnapshotConflict(["bad@example.com"]),
        ), patch(
            "webui.app.db.get_account_by_email", return_value={"id": 1, "email": "bad@example.com"}
        ), patch(
            "webui.app.codex_retry_service.reserve", return_value=True
        ), patch(
            "webui.app.codex_retry_service.release"
        ) as release:
            response = self.client.post(
                "/api/cpa-401/retry",
                headers=self.headers,
                json={"emails": ["bad@example.com"]},
            )
        self.assertEqual(409, response.status_code)
        self.assert_sensitive_headers(response)
        self.assertEqual(["bad@example.com"], response.get_json()["conflicts"])
        release.assert_called_once_with("bad@example.com")

    def test_retry_all_skipped_is_422_with_sensitive_headers(self):
        with patch(
            "core.cpa_401_service.Cpa401Service.select_retryable",
            return_value=([], [{"email": "missing@example.com", "reason": "不在最近一次检测结果中"}]),
        ):
            response = self.client.post(
                "/api/cpa-401/retry", headers=self.headers, json={"emails": ["missing@example.com"]}
            )
        self.assertEqual(422, response.status_code)
        self.assert_sensitive_headers(response)
        self.assertEqual(0, response.get_json()["started_count"])

    def test_cleanup_unavailable_returns_exact_review_queue_without_deletion(self):
        payload = {
            "ok": True,
            "cleanup_supported": False,
            "cleanup_performed": False,
            "manual_action": "仅生成清单",
            "items": [{
                "email": "bad@example.com",
                "replacement_file": "new.json",
                "cleanup_files": ["old.json"],
            }],
        }
        with patch("core.cpa_401_service.Cpa401Service.cleanup", return_value=payload) as cleanup:
            response = self.client.post(
                "/api/cpa-401/cleanup",
                headers=self.headers,
                json={"emails": ["bad@example.com"], "confirm_cleanup": True},
            )
        self.assertEqual(200, response.status_code)
        self.assert_sensitive_headers(response)
        body = response.get_json()
        self.assertFalse(body["cleanup_supported"])
        self.assertFalse(body["cleanup_performed"])
        self.assertEqual(["old.json"], body["items"][0]["cleanup_files"])
        cleanup.assert_called_once_with(["bad@example.com"], opt_in=True, limit=50)

    def test_cpa_batch_request_validation_is_bounded(self):
        cases = [
            ("/api/cpa-401/retry", None),
            ("/api/cpa-401/retry", {"emails": []}),
            ("/api/cpa-401/retry", {"emails": ["a@example.com"] * 51}),
            ("/api/cpa-401/cleanup", None),
            ("/api/cpa-401/cleanup", {"emails": []}),
            ("/api/cpa-401/cleanup", {"emails": ["a@example.com"] * 51}),
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
