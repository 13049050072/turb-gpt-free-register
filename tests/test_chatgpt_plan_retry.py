# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import core.chatgpt_plan as plan


class _Response:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text
        self.headers = {}

    def json(self):
        return self._data


class _HttpSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _PlanSession:
    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.device_id = "device-one"
        self.oai_session_id = "session-one"
        self.session = _HttpSession()
        self.reset_count = 0
        self.requests = []
        self.responses = [
            _Response(403, text="blocked"),
            _Response(200, data={
                "accounts": {
                    "acc-1": {
                        "account": {"account_id": "acc-1", "plan_type": "free"},
                        "entitlement": {},
                    }
                }
            }),
        ]
        self.created.append(self)

    def get_chatgpt_headers(self, **_kwargs):
        return {
            "content-type": "application/json",
            "oai-device-id": self.device_id,
            "oai-session-id": self.oai_session_id,
            "oai-client-build-number": "build",
            "oai-client-version": "version",
        }

    def get(self, url, headers=None, **_kwargs):
        self.requests.append((url, dict(headers or {})))
        return self.responses.pop(0)

    def reset_circuit_breaker(self):
        self.reset_count += 1

    def js_timezone_offset_min(self):
        return -540

    def fingerprint_summary_text(self):
        return "lang=ja-JP tz=Asia/Tokyo"


class ChatgptPlanRetryTests(unittest.TestCase):
    def setUp(self):
        _PlanSession.created = []

    def test_403_retries_in_same_session_with_complete_frontend_headers(self):
        claims = {
            "payload": {}, "email": "one@example.com", "account_id": "acc-1",
            "token_expired": False, "claim_plan_type": "free",
        }
        with patch.object(plan, "BrowserSession", _PlanSession), patch.object(
            plan, "token_claims", return_value=claims
        ), patch.object(plan, "resolve_plan_check_route", return_value={
            "proxy": "socks5://proxy:1080", "proxy_mode": "proxy",
            "network_route": "proxy", "proxy_used": "socks5://***:***@proxy:1080",
            "proxy_fallback_reason": None,
        }), patch.object(plan, "_warm_plan_session"), patch.object(
            plan.time, "sleep"
        ):
            result = plan.check_account_plan(
                "token", max_attempts=2, retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["timezone_offset_min"], "-540")
        self.assertEqual(len(_PlanSession.created), 1)
        session = _PlanSession.created[0]
        self.assertEqual(session.reset_count, 1)
        self.assertTrue(session.session.closed)
        self.assertTrue(session.kwargs["detect_exit_geo"])
        self.assertTrue(session.kwargs["fingerprint_seed"].startswith("plan-check:one@example.com:"))
        first_headers = session.requests[0][1]
        self.assertEqual(first_headers["chatgpt-account-id"], "acc-1")
        self.assertEqual(first_headers["oai-device-id"], "device-one")
        self.assertEqual(first_headers["oai-session-id"], "session-one")
        self.assertNotIn("content-type", first_headers)

    def test_owned_plan_session_retries_login_navigation_and_uses_observed_build(self):
        from core import openai_auth

        class Response(_Response):
            url = "https://chatgpt.com/auth/login"

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

        class Session(_PlanSession):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.events = []
                self.client_build_number = "stale-build"
                self.responses = [
                    Response(403),
                    Response(200),
                    Response(200, data={"accounts": {"acc-1": {
                        "account": {"account_id": "acc-1", "plan_type": "free"},
                        "entitlement": {},
                    }}}),
                ]

            def get_chatgpt_navigate_headers(self, **kwargs):
                self.events.append(("navigate_headers", kwargs))
                return {"sec-fetch-site": "none"}

            def observe_chatgpt_document(self, response):
                self.events.append(("observe", response.url))
                self.client_build_number = "observed-build"

            def get_chatgpt_headers(self, **_kwargs):
                return {"oai-client-build-number": self.client_build_number}

            def get(self, url, headers=None, **kwargs):
                self.events.append(("get", url, dict(headers or {})))
                self.requests.append((url, dict(headers or {})))
                return self.responses.pop(0)

        claims = {
            "payload": {}, "email": "one@example.com", "account_id": "acc-1",
            "token_expired": False, "claim_plan_type": "free",
        }
        with patch.object(plan, "BrowserSession", Session), patch.object(
            plan, "token_claims", return_value=claims
        ), patch.object(plan, "resolve_plan_check_route", return_value={
            "proxy": None, "proxy_mode": "direct", "network_route": "direct",
            "proxy_used": None, "proxy_fallback_reason": None,
        }), patch.object(openai_auth, "_interruptible_sleep") as sleep:
            result = plan.check_account_plan("token", max_attempts=1)

        self.assertTrue(result["ok"])
        session = Session.created[-1]
        self.assertEqual([event[1] for event in session.events if event[0] == "get"], [
            "https://chatgpt.com/auth/login",
            "https://chatgpt.com/auth/login",
            "https://chatgpt.com" + plan.ACCOUNTS_CHECK_PATH + "?timezone_offset_min=-540",
        ])
        self.assertEqual(session.events[-2][0], "observe")
        self.assertEqual(session.requests[-1][1]["oai-client-build-number"], "observed-build")
        self.assertEqual(session.reset_count, 2)
        self.assertTrue(session.session.closed)
        sleep.assert_called_once_with(1.0)

    def test_failed_warmup_is_best_effort_and_borrowed_sessions_are_not_warmed_or_closed(self):
        class BorrowedSession(_PlanSession):
            def __init__(self):
                super().__init__()
                self.closed = False
                self.proxy = ""

            def close(self):
                self.closed = True

        borrowed = BorrowedSession()
        borrowed.responses = [_Response(200, data={"accounts": {"acc-1": {
            "account": {"account_id": "acc-1", "plan_type": "free"},
            "entitlement": {},
        }}})]
        claims = {
            "payload": {}, "email": "one@example.com", "account_id": "acc-1",
            "token_expired": False, "claim_plan_type": "free",
        }
        with patch.object(plan, "token_claims", return_value=claims), patch.object(
            plan, "_warm_plan_session", side_effect=AssertionError("borrowed session warmed")
        ):
            result = plan.check_account_plan("token", session=borrowed, max_attempts=1)
        self.assertTrue(result["ok"])
        self.assertFalse(borrowed.closed)
        self.assertEqual(len(borrowed.requests), 1)

        owned = _PlanSession()
        owned.responses = [_Response(200, data={"accounts": {"acc-1": {
            "account": {"account_id": "acc-1", "plan_type": "free"},
            "entitlement": {},
        }}})]
        with patch.object(plan, "BrowserSession", return_value=owned), patch.object(
            plan, "token_claims", return_value=claims
        ), patch.object(plan, "resolve_plan_check_route", return_value={
            "proxy": None, "proxy_mode": "direct", "network_route": "direct",
            "proxy_used": None, "proxy_fallback_reason": None,
        }), patch(
            "core.openai_auth.network_preflight", side_effect=RuntimeError("warmup failed")
        ):
            result = plan.check_account_plan("token", max_attempts=1)
        self.assertTrue(result["ok"])
        self.assertEqual(owned.reset_count, 1)
        self.assertTrue(owned.session.closed)

    def test_403_is_retryable(self):
        self.assertTrue(plan._retryable_plan_error(403))
        self.assertFalse(plan._retryable_plan_error(401))


if __name__ == "__main__":
    unittest.main()
