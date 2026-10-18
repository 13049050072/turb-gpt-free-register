# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import core.account_liveness as liveness
import core.live_check_service as live_service
import core.proxy_utils as proxy_utils
from core.error_redaction import redact_error
from core.proxy_utils import is_1024proxy_dynamic_gateway, liveness_proxy_url


class _DummyHttpSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _DummyBrowserSession:
    created = []

    def __init__(self, proxy=None, **kwargs):
        self.proxy = proxy
        self.received_proxy = proxy
        self.device_id = "device-test"
        self.session = _DummyHttpSession()
        self.blocked_until = 0.0
        self.blocked_reason = ""
        self.created.append(self)
        self.kwargs = kwargs

    def fingerprint_summary(self):
        return {
            "proxy": self.proxy or "",
            "user_agent": "test-agent",
            "accept_language": "en-US",
            "timezone_iana": "UTC",
            "timezone_offset_minutes": 0,
            "screen_width": 1440,
            "screen_height": 900,
            "device_pixel_ratio": 2,
            "hardware_concurrency": 8,
            "device_memory": 8,
            "geo_country": "US",
            "geo_city": "Test",
        }

    def fingerprint_summary_text(self):
        return "test-fingerprint"

    def reset_circuit_breaker(self):
        self.blocked_until = 0.0
        self.blocked_reason = ""


class _DummyQueueSlot:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class AccountLivenessTests(unittest.TestCase):
    def setUp(self):
        _DummyBrowserSession.created = []

    def test_1024proxy_sticky_url_is_bounded_per_account_and_attempt(self):
        template = "http://user%40name:p%3Ass@us.1024proxy.io:8080"
        first = liveness_proxy_url(
            template, account_key="one@example.test", route_attempt=1,
            run_nonce="run-one",
        )
        same = liveness_proxy_url(
            template, account_key="one@example.test", route_attempt=1,
            run_nonce="run-one",
        )
        second = liveness_proxy_url(
            template, account_key="one@example.test", route_attempt=2,
            run_nonce="run-one",
        )
        other = liveness_proxy_url(
            template, account_key="two@example.test", route_attempt=1,
            run_nonce="run-one",
        )
        fresh_run = liveness_proxy_url(
            template, account_key="one@example.test", route_attempt=1,
            run_nonce="run-two",
        )

        self.assertEqual(first, same)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, fresh_run)
        self.assertIn("-sid-", first)
        self.assertIn("-t-10", first)
        self.assertIn("user%40name", first)
        self.assertIn(":p%3Ass@", first)
        self.assertLessEqual(len(first.split("-sid-", 1)[1].split("-t-", 1)[0]), 20)

    def test_1024proxy_default_nonce_is_fresh_per_helper_invocation(self):
        template = "http://user:pass@us.1024proxy.io:8080"
        with patch.object(proxy_utils.secrets, "token_hex", side_effect=["nonce-one", "nonce-two"]):
            first = liveness_proxy_url(
                template, account_key="one@example.test", route_attempt=1,
            )
            second = liveness_proxy_url(
                template, account_key="one@example.test", route_attempt=1,
            )

        self.assertNotEqual(first, second)

    def test_1024proxy_host_suffix_matching_is_boundary_aware(self):
        for suffix in ("io", "com", "net"):
            self.assertTrue(is_1024proxy_dynamic_gateway(f"http://user:pass@us.1024proxy.{suffix}:8080"))
        self.assertFalse(is_1024proxy_dynamic_gateway("http://user:pass@us.1024proxy.io.evil:8080"))
        self.assertFalse(is_1024proxy_dynamic_gateway("http://user:pass@not-1024proxy.io:8080"))

    def test_1024proxy_existing_sticky_url_is_unchanged(self):
        value = "http://user-sid-provided-t-30:pass@us.1024proxy.io:8080"
        self.assertEqual(
            liveness_proxy_url(value, account_key="one@example.test", route_attempt=9),
            value,
        )

    def test_non_1024_proxy_and_direct_mode_are_unchanged(self):
        for value in ("", "http://user:pass@proxy.example:8080"):
            self.assertEqual(
                liveness_proxy_url(value, account_key="one@example.test", route_attempt=1),
                value,
            )

    def test_direct_proxy_mode_does_not_select_a_configured_pool(self):
        cfg = SimpleNamespace(
            PLAN_CHECK_PROXY_MODE="direct",
            PLAN_CHECK_PROXY=["http://user:pass@us.1024proxy.io:8080"],
            PROXY_POOL=["http://user:pass@proxy.example:8080"],
        )
        self.assertEqual(live_service._configured_live_check_proxies(cfg), [])

    def test_generated_session_ids_are_redacted_from_error_text(self):
        session_id = "a" * 20
        text = redact_error(f"proxy failed -sid-{session_id}-t-10")
        self.assertNotIn(session_id, text)
        self.assertIn("-sid-[redacted]-t-[redacted]", text)

    def test_preflight_preserves_explicit_direct_route_and_skips_providers(self):
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "probe_auth_session"), \
             patch.object(liveness, "get_csrf_token", return_value="csrf"), \
             patch.object(liveness, "signin_openai", return_value="https://auth.example/authorize"):
            session, authorize_url = liveness._network_preflight_with_retry(
                "user@example.com", "", max_attempts=1
            )

        self.assertEqual(session.proxy, "")
        self.assertEqual(authorize_url, "https://auth.example/authorize")
        self.assertTrue(
            session.kwargs["fingerprint_seed"].startswith("live-check:user@example.com:")
        )

    def test_preflight_retries_with_same_session_when_csrf_is_blocked(self):
        csrf_errors = [RuntimeError("HTTP Error 403"), "csrf"]
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "probe_auth_session"), \
             patch.object(liveness, "get_csrf_token", side_effect=csrf_errors), \
             patch.object(liveness, "signin_openai", return_value="authorize"), \
             patch.object(liveness.time, "sleep"):
            session, _ = liveness._network_preflight_with_retry(
                "user@example.com", None, max_attempts=2
            )

        self.assertIs(_DummyBrowserSession.created[-1], session)
        # 403 下发的 CF Cookie 必须留在同一 Cookie Jar 中，不能每轮新建会话。
        self.assertEqual(len(_DummyBrowserSession.created), 1)
        self.assertFalse(_DummyBrowserSession.created[0].session.closed)
        self.assertIsNone(_DummyBrowserSession.created[0].received_proxy)

    def test_callback_403_retries_in_same_session_before_fetching_token(self):
        session = _DummyBrowserSession(proxy="proxy")
        with patch.object(
            liveness,
            "follow_oauth_callback",
            side_effect=[RuntimeError("HTTP 403 from callback/openai"), "https://chatgpt.com/"],
        ) as callback, patch.object(
            liveness,
            "fetch_session",
            return_value={"accessToken": "token"},
        ) as fetch, patch.object(liveness.time, "sleep"):
            result = liveness._follow_continue_and_fetch(
                session,
                "https://auth.openai.com/authorize/continue?state=test",
                referer="https://auth.openai.com/email-verification",
            )

        self.assertEqual(result["accessToken"], "token")
        self.assertEqual(callback.call_count, 2)
        fetch.assert_called_once_with(session)

    def test_explicit_empty_proxy_stays_direct_even_when_pool_is_configured(self):
        slot = _DummyQueueSlot()
        gateway = "http://user:pass@us.1024proxy.io:8080"
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "_configured_live_check_proxies", return_value=[gateway]), \
             patch.object(live_service, "resolve_plan_check_route", return_value={
                 "proxy": "", "network_route": "direct", "proxy_mode": "request",
             }), \
             patch.object(live_service, "check_account_liveness", return_value=success) as check:
            result = live_service._run_live_check(
                account_id=9, email="user@example.com", proxy="", trigger="manual",
            )

        self.assertTrue(result["ok"])
        check.assert_called_once()
        self.assertEqual(check.call_args.kwargs["proxy"], "")
        self.assertTrue(slot.released)

        session = _DummyBrowserSession(proxy="")
        challenge = {
            "continue_url": "https://auth.openai.com/mfa-challenge/factor-1",
            "page": {
                "type": "mfa_challenge",
                "payload": {"factor_id": "factor-1"},
            },
        }
        with patch.object(liveness, "_validate_with_retry", return_value=challenge), \
             patch.object(liveness, "_account_totp_secret", return_value="totp-secret"), \
             patch.object(liveness, "_account_totp_code", return_value="123456"), \
             patch.object(liveness, "_mfa_issue_challenge") as issue, \
             patch.object(liveness, "_mfa_verify", return_value={
                 "continue_url": "https://auth.openai.com/authorize/continue?state=ok",
             }) as verify, \
             patch.object(liveness, "_follow_continue_and_fetch", return_value={
                 "accessToken": "fresh-token",
             }) as follow:
            result = liveness._login_via_email_otp(session, "user@example.com", 1.0)

        self.assertEqual(result["accessToken"], "fresh-token")
        issue.assert_called_once_with(session, "factor-1")
        verify.assert_called_once_with(session, "factor-1", "123456")
        follow.assert_called_once_with(
            session,
            "https://auth.openai.com/authorize/continue?state=ok",
            referer="https://auth.openai.com/mfa-challenge/factor-1",
        )

    def test_fingerprint_identity_is_pinned_when_session_is_recreated_in_one_attempt(self):
        state = {}
        first = MagicMock()
        first.browser_profile = {
            "navigator_language": "ja-JP",
            "timezone_iana": "Asia/Tokyo",
            "screen_width": 1512,
        }
        second = MagicMock()
        second.browser_profile = dict(first.browser_profile)
        with patch.object(liveness, "BrowserSession", side_effect=[first, second]) as factory:
            self.assertIs(
                liveness._new_fingerprint_pinned_session(
                    "user@example.com", "socks5://proxy:1080", state,
                ),
                first,
            )
            self.assertIs(
                liveness._new_fingerprint_pinned_session(
                    "user@example.com", "", state,
                ),
                second,
            )

        first_kwargs = factory.call_args_list[0].kwargs
        second_kwargs = factory.call_args_list[1].kwargs
        self.assertTrue(first_kwargs["detect_exit_geo"])
        self.assertIsNone(first_kwargs["browser_profile"])
        self.assertFalse(second_kwargs["detect_exit_geo"])
        self.assertEqual(second_kwargs["browser_profile"]["navigator_language"], "ja-JP")
        self.assertEqual(second_kwargs["browser_profile"]["timezone_iana"], "Asia/Tokyo")
        self.assertEqual(first_kwargs["fingerprint_seed"], second_kwargs["fingerprint_seed"])
        self.assertTrue(str(first_kwargs["fingerprint_seed"]).startswith("live-check:user@example.com:"))

    def test_reauth_otp_dead_account_error_is_not_retried(self):
        response = SimpleNamespace(
            status_code=403,
            text='{"error":{"code":"account_deactivated"}}',
        )
        error = RuntimeError("HTTP Error 403")
        error.response = response
        session = _DummyBrowserSession(proxy="proxy")

        with patch.object(liveness, "_validate_reauth_otp", side_effect=error), \
             patch.object(liveness, "wait_for_otp", return_value="123456"):
            with self.assertRaises(liveness.AccountUnusableError) as ctx:
                liveness._validate_reauth_with_retry(session, "user@example.com", 1.0)

        self.assertEqual(ctx.exception.error_code, "account_deactivated")

    def test_existing_access_token_uses_reauth_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy="")
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "BrowserSession", return_value=session), \
                 patch.object(liveness, "_warm_authenticated_session") as warm, \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", return_value={
                     "accessToken": "new-token",
                     "user": {"id": "user-1"},
                     "account": {"planType": "free"},
                 }) as reauth:
                result = liveness.check_account_liveness("user@example.com", proxy="")

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        warm.assert_called_once_with(session, "old-token")
        reauth.assert_called_once()
        self.assertTrue(session.session.closed)

    def test_reauth_403_falls_back_to_clean_full_web_login_on_same_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            reauth_session = _DummyBrowserSession(proxy="socks5://proxy.example:1080")
            full_session = _DummyBrowserSession(proxy="socks5://proxy.example:1080")
            fresh_info = {
                "accessToken": "new-token",
                "user": {"id": "user-1"},
                "account": {"planType": "free"},
            }
            state = {}
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "_account_totp_secret", return_value=""), \
                 patch.object(liveness, "_new_fingerprint_pinned_session", return_value=reauth_session), \
                 patch.object(liveness, "_warm_authenticated_session"), \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", side_effect=RuntimeError("HTTP 403 callback")), \
                 patch.object(liveness, "_login_via_full_web_flow", return_value=(
                     full_session, fresh_info,
                 )) as full_login:
                result = liveness.check_account_liveness(
                    "user@example.com",
                    proxy=None,
                    fingerprint_state=state,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        full_login.assert_called_once_with(
            "user@example.com",
            "socks5://proxy.example:1080",
            email_source=None,
            fingerprint_state=state,
        )
        self.assertTrue(reauth_session.session.closed)
        self.assertTrue(full_session.session.closed)

    def test_service_403_fallback_really_uses_direct_connection(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", return_value={
                 "proxy": "socks5://proxy.example:1080",
                 "network_route": "proxy",
                 "proxy_mode": "auto",
             }), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, success]) as check:
            result = live_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], "socks5://proxy.example:1080")
        self.assertEqual(check.call_args_list[0].kwargs["email_source"], "remail")
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], "")
        self.assertEqual(check.call_args_list[1].kwargs["email_source"], "remail")
        self.assertIsNot(
            check.call_args_list[0].kwargs["fingerprint_state"],
            check.call_args_list[1].kwargs["fingerprint_state"],
        )
        self.assertTrue(slot.released)


    def test_tls_transport_error_is_retryable_for_direct_fallback(self):
        self.assertTrue(
            liveness._is_retryable_network_error(
                RuntimeError("SSLError: curl: (35) TLS connect error: WRONG_VERSION_NUMBER")
            )
        )

    def test_service_retries_single_1024proxy_gateway_with_fresh_sticky_session(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        gateway = "http://acct%40label:pw%3A%40%2F@us.1024proxy.io:8080"

        def resolve_route(explicit_proxy=None):
            selected = str(explicit_proxy or "").strip()
            return {
                "proxy": selected,
                "network_route": "proxy" if selected else "direct",
                "proxy_mode": "request" if selected else "auto",
                "proxy_used": live_service._mask_proxy(selected) or None,
                "upstream_proxy": "",
                "upstream_proxy_used": None,
                "proxy_fallback_reason": None,
            }

        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "_configured_live_check_proxies", return_value=[gateway]), \
             patch.object(live_service, "resolve_plan_check_route", side_effect=resolve_route), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, success]) as check:
            result = live_service._run_live_check(
                account_id=4, email="user@example.com", proxy=None, trigger="manual",
            )

        self.assertTrue(result["ok"])
        first = check.call_args_list[0].kwargs["proxy"]
        second = check.call_args_list[1].kwargs["proxy"]
        self.assertIn("-sid-", first)
        self.assertIn("acct%40label-sid-", first)
        self.assertIn(":pw%3A%40%2F@", first)
        self.assertIn("-sid-", second)
        self.assertIn("acct%40label-sid-", second)
        self.assertNotEqual(first, second)
        self.assertEqual(result["proxy_used"], "http://***:***@us.1024proxy.io:8080")
        self.assertNotIn("-sid-", result["proxy_used"])
        self.assertNotIn("pw%3A%40%2F", result["proxy_used"])
        self.assertTrue(slot.released)

    def test_service_honors_the_configured_dynamic_route_attempt_limit(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        gateway = "http://acct%40label:pw%3A%40%2F@us.1024proxy.io:8080"

        def resolve_route(explicit_proxy=None):
            selected = str(explicit_proxy or "").strip()
            return {"proxy": selected, "network_route": "proxy" if selected else "direct"}

        with patch("config.proxy.PLAN_CHECK_MAX_ATTEMPTS", 2), \
             patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "_configured_live_check_proxies", return_value=[gateway]), \
             patch.object(live_service, "resolve_plan_check_route", side_effect=resolve_route), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, failed]) as check:
            result = live_service._run_live_check(
                account_id=7, email="user@example.com", proxy=None, trigger="manual",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(check.call_count, 2)
        self.assertNotEqual(
            check.call_args_list[0].kwargs["proxy"],
            check.call_args_list[1].kwargs["proxy"],
        )
        self.assertTrue(slot.released)

    def test_service_switches_to_an_unused_dynamic_gateway_before_same_template_retry(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        first_gateway = "http://acct:pw@us.1024proxy.io:8080"
        second_gateway = "http://acct:pw@de.1024proxy.com:8080"

        def resolve_route(explicit_proxy=None):
            selected = str(explicit_proxy or "").strip()
            return {"proxy": selected, "network_route": "proxy" if selected else "direct"}

        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "_configured_live_check_proxies", return_value=[first_gateway, second_gateway]), \
             patch.object(live_service.random, "choice", side_effect=[first_gateway, second_gateway]), \
             patch.object(live_service, "resolve_plan_check_route", side_effect=resolve_route), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, success]) as check:
            result = live_service._run_live_check(
                account_id=8, email="user@example.com", proxy=None, trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertIn("us.1024proxy.io", check.call_args_list[0].kwargs["proxy"])
        self.assertIn("de.1024proxy.com", check.call_args_list[1].kwargs["proxy"])
        self.assertTrue(slot.released)

    def test_service_keeps_existing_sticky_route_fallback_behavior(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        sticky = "http://acct-sid-provided-t-30:pw%3A%40@us.1024proxy.io:8080"
        logged = []

        def resolve_route(explicit_proxy=None):
            selected = str(explicit_proxy or "").strip()
            return {
                "proxy": selected,
                "network_route": "proxy" if selected else "direct",
                "proxy_mode": "request" if selected else "auto",
                "proxy_used": live_service._mask_proxy(selected) or None,
                "upstream_proxy": "",
                "upstream_proxy_used": None,
                "proxy_fallback_reason": None,
            }

        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_safe_append_log", side_effect=lambda email, line, **kwargs: logged.append(line)), \
             patch.object(live_service, "_configured_live_check_proxies", return_value=[sticky]), \
             patch.object(live_service, "resolve_plan_check_route", side_effect=resolve_route), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, success]) as check:
            result = live_service._run_live_check(
                account_id=6, email="user@example.com", proxy=None, trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], sticky)
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], "")
        self.assertEqual(len(check.call_args_list), 2)
        self.assertFalse(any("保留网关模板并创建新的会话" in str(line) for line in logged))
        self.assertTrue(slot.released)

    def test_definitive_account_failure_does_not_retry(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "deactivated", "error": "account_deactivated"}
        gateway = "http://user:pass@us.1024proxy.io:8080"
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "_configured_live_check_proxies", return_value=[gateway]), \
             patch.object(live_service, "resolve_plan_check_route", return_value={"proxy": "", "network_route": "direct", "proxy_mode": "auto"}), \
             patch.object(live_service, "check_account_liveness", return_value=failed) as check:
            result = live_service._run_live_check(
                account_id=5, email="user@example.com", proxy=None, trigger="manual",
            )

        self.assertEqual(result["status"], "deactivated")
        check.assert_called_once()
        self.assertTrue(slot.released)

    def test_definitive_account_error_in_403_text_does_not_retry(self):
        self.assertFalse(
            live_service._retryable_live_check_result({
                "ok": False,
                "status": "failed",
                "error": "HTTP Error 403: account_deactivated",
            })
        )

    def test_service_tls_fallback_really_uses_direct_connection(self):
        slot = _DummyQueueSlot()
        failed = {
            "ok": False,
            "status": "failed",
            "error": "SSLError: curl: (35) TLS connect error: WRONG_VERSION_NUMBER",
        }
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "generic_api"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", return_value={
                 "proxy": "http://proxy.example:3000",
                 "network_route": "proxy",
                 "proxy_mode": "proxy",
             }), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, success]) as check:
            result = live_service._run_live_check(
                account_id=3,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], "http://proxy.example:3000")
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], "")
        self.assertTrue(slot.released)

if __name__ == "__main__":
    unittest.main()
