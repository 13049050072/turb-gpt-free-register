# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.chatgpt2api_admin import ChatGPT2APIAdminError
from core.chatgpt2api_401_service import (
    CHATGPT2API_401_MAX_ITEMS,
    ChatGPT2API401BusyError,
)
from webui.app import create_app


class WebUIChatGPT2API401Tests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def assert_private(self, response):
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual("no-cache", response.headers.get("Pragma"))
        self.assertEqual("nosniff", response.headers.get("X-Content-Type-Options"))

    def test_page_and_routes_require_authentication(self):
        self.assertEqual(302, self.client.get("/chatgpt2api-401").status_code)
        for method, path in (
            ("get", "/api/chatgpt2api-401/state"),
            ("post", "/api/chatgpt2api-401/detect"),
            ("post", "/api/chatgpt2api-401/refresh"),
            ("post", "/api/chatgpt2api-401/replace"),
        ):
            with self.subTest(path=path):
                self.assertEqual(401, getattr(self.client, method)(path).status_code)
        page = self.client.get("/chatgpt2api-401", headers=self.headers)
        self.assertEqual(200, page.status_code)
        self.assertIn("ChatGPT2API 401 AT", page.get_data(as_text=True))

    def test_state_and_detect_are_private_and_never_echo_credentials(self):
        payload = {
            "ok": True,
            "items": [{
                "remote_id": "remote-old", "email": "one@example.com", "local_account_id": 7,
                "state": "pending", "reason": "HTTP 401 unauthorized",
                "old_at_fingerprint": "0123456789ab", "new_at_fingerprint": "",
                "retryable": True, "replaceable": False,
            }],
            "counts": {"invalid": 1, "pending": 1, "healthy": 2},
            "checked_at": "2026-10-12T00:00:00+00:00",
        }
        with patch("core.chatgpt2api_401_service.ChatGPT2API401Service.state", return_value=payload):
            state = self.client.get("/api/chatgpt2api-401/state", headers=self.headers)
        with patch("core.chatgpt2api_401_service.ChatGPT2API401Service.detect", return_value=payload):
            detect = self.client.post("/api/chatgpt2api-401/detect", headers=self.headers)
        for response in (state, detect):
            self.assertEqual(200, response.status_code)
            self.assert_private(response)
            text = response.get_data(as_text=True).lower()
            self.assertNotIn("access_token", text)
            self.assertNotIn("refresh_token", text)
            self.assertNotIn("callback", text)
            self.assertEqual("remote-old", response.get_json()["items"][0]["remote_id"])

    def test_refresh_requires_confirmation_returns_202_and_passes_only_locked_ids(self):
        payload = {"ok": True, "queued_count": 1, "workers": 5, "skipped": []}
        with patch("core.chatgpt2api_401_service.ChatGPT2API401Service.refresh", return_value=payload) as refresh:
            response = self.client.post(
                "/api/chatgpt2api-401/refresh",
                headers=self.headers,
                json={"remote_ids": ["remote-old"], "confirm_replace": True},
            )
        self.assertEqual(202, response.status_code)
        self.assert_private(response)
        refresh.assert_called_once_with(
            ["remote-old"],
            opt_in=True,
            limit=CHATGPT2API_401_MAX_ITEMS,
        )

        for confirmation in (None, False, "true", 1):
            with self.subTest(confirmation=confirmation), patch(
                "core.chatgpt2api_401_service.ChatGPT2API401Service.refresh"
            ) as refresh:
                body = {"remote_ids": ["remote-old"]}
                if confirmation is not None:
                    body["confirm_replace"] = confirmation
                rejected = self.client.post(
                    "/api/chatgpt2api-401/refresh",
                    headers=self.headers,
                    json=body,
                )
            self.assertEqual(400, rejected.status_code)
            refresh.assert_not_called()

    def test_replace_requires_literal_confirmation_and_passes_locked_ids(self):
        preview = {"ok": True, "replacement_performed": False, "items": []}
        with patch("core.chatgpt2api_401_service.ChatGPT2API401Service.replace", return_value=preview) as replace:
            response = self.client.post(
                "/api/chatgpt2api-401/replace",
                headers=self.headers,
                json={"remote_ids": ["remote-old"], "confirm_replace": "true"},
            )
        self.assertEqual(200, response.status_code)
        replace.assert_called_once_with(
            ["remote-old"], opt_in=False, limit=CHATGPT2API_401_MAX_ITEMS
        )

        with patch("core.chatgpt2api_401_service.ChatGPT2API401Service.replace", return_value=preview) as replace:
            response = self.client.post(
                "/api/chatgpt2api-401/replace",
                headers=self.headers,
                json={"remote_ids": ["remote-old"], "confirm_replace": True},
            )
        self.assertEqual(200, response.status_code)
        self.assert_private(response)
        replace.assert_called_once_with(
            ["remote-old"], opt_in=True, limit=CHATGPT2API_401_MAX_ITEMS
        )

    def test_batch_request_validation_is_bounded(self):
        too_many = [f"remote-{index}" for index in range(CHATGPT2API_401_MAX_ITEMS + 1)]
        cases = [
            ("/api/chatgpt2api-401/refresh", None),
            ("/api/chatgpt2api-401/refresh", {}),
            ("/api/chatgpt2api-401/refresh", {"remote_ids": [], "confirm_replace": True}),
            ("/api/chatgpt2api-401/refresh", {"remote_ids": too_many, "confirm_replace": True}),
            ("/api/chatgpt2api-401/refresh", {"remote_ids": [""], "confirm_replace": True}),
            ("/api/chatgpt2api-401/refresh", {"remote_ids": [7], "confirm_replace": True}),
            ("/api/chatgpt2api-401/refresh", {"remote_ids": ["x" * 161], "confirm_replace": True}),
            ("/api/chatgpt2api-401/replace", None),
            ("/api/chatgpt2api-401/replace", {"remote_ids": []}),
            ("/api/chatgpt2api-401/replace", {"remote_ids": too_many}),
        ]
        for path, payload in cases:
            with self.subTest(path=path, payload=payload):
                kwargs = {"headers": self.headers}
                if payload is None:
                    kwargs.update({"data": "[]", "content_type": "application/json"})
                else:
                    kwargs["json"] = payload
                response = self.client.post(path, **kwargs)
                self.assertEqual(400, response.status_code)
                self.assert_private(response)

    def test_more_than_fifty_ids_are_accepted_for_queueing(self):
        remote_ids = [f"remote-{index}" for index in range(51)]
        payload = {"ok": True, "queued_count": 51, "workers": 5, "skipped": []}
        with patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.refresh",
            return_value=payload,
        ) as refresh:
            response = self.client.post(
                "/api/chatgpt2api-401/refresh",
                headers=self.headers,
                json={"remote_ids": remote_ids, "confirm_replace": True},
            )
        self.assertEqual(202, response.status_code)
        self.assertEqual(51, response.get_json()["queued_count"])
        refresh.assert_called_once_with(
            remote_ids,
            opt_in=True,
            limit=CHATGPT2API_401_MAX_ITEMS,
        )

    def test_detect_reports_active_queue_as_conflict(self):
        with patch(
            "core.chatgpt2api_401_service.ChatGPT2API401Service.detect",
            side_effect=ChatGPT2API401BusyError("queue active"),
        ):
            response = self.client.post("/api/chatgpt2api-401/detect", headers=self.headers)
        self.assertEqual(409, response.status_code)
        self.assertEqual("queue active", response.get_json()["error"])
        self.assert_private(response)

    def test_remote_errors_are_bounded_and_redacted(self):
        cases = [
            (ChatGPT2APIAdminError("secret", status_code=502, category="auth"), 502, "认证失败"),
            (ChatGPT2APIAdminError("secret", status_code=504, category="timeout"), 504, "超时"),
            (ChatGPT2APIAdminError("secret", status_code=501, category="unsupported"), 501, "不支持"),
        ]
        for error, expected_status, expected_text in cases:
            with self.subTest(category=error.category), patch(
                "core.chatgpt2api_401_service.ChatGPT2API401Service.detect", side_effect=error
            ):
                response = self.client.post("/api/chatgpt2api-401/detect", headers=self.headers)
            self.assertEqual(expected_status, response.status_code)
            self.assert_private(response)
            self.assertIn(expected_text, response.get_json()["error"])
            self.assertNotIn("secret", response.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main()
