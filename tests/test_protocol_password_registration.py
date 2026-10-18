# -*- coding: utf-8 -*-
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import main


class ProtocolPasswordRegistrationTests(unittest.TestCase):
    def test_password_is_submitted_before_registration_otp(self):
        session = SimpleNamespace(
            proxy=None,
            device_id="device-test",
            auth_session_logging_id="auth-log-test",
            browser_profile={},
            sentinel_sid="sentinel-test",
        )
        events = []
        saved = {}
        tracker_box = {}

        def observe_request(tracker, url):
            tracker.observe(
                method="GET",
                url=url,
                response=SimpleNamespace(
                    status_code=200,
                    request_size=10,
                    upload_size=0,
                    download_size=4,
                    header_size=6,
                    response_size=10,
                    elapsed=0.1,
                    redirect_count=0,
                ),
            )

        def authorize(_session, *_args, **kwargs):
            tracker_box["tracker"] = kwargs["traffic_tracker"]
            observe_request(kwargs["traffic_tracker"], "https://auth.openai.com/authorize")
            return _session, "https://auth.openai.com/email-verification"

        def password_page(*_args):
            events.append("password_page")
            return "https://auth.openai.com/create-account/password"

        def password_bundle(*_args):
            events.append("password_bundle")
            return {"token": "password-challenge"}

        def sentinel_header(_session, _challenge, flow):
            events.append(f"sentinel:{flow}")
            return "sentinel-header", "so-header"

        def register(*args, **_kwargs):
            events.append("register_user")
            self.assertEqual(args[1], "user@example.com")
            self.assertEqual(args[2], "ValidPassword9!")
            return {"continue_url": "/api/accounts/email-otp/send"}

        def send_otp(*_args):
            events.append("otp_send")
            return "https://auth.openai.com/email-verification"

        def validate(*_args, **_kwargs):
            events.append("otp_validate")
            return {
                "page": {"type": "about_you"},
                "continue_url": "https://auth.openai.com/about-you",
            }

        def create(*_args, **_kwargs):
            events.append("create_account")
            return {"continue_url": "https://chatgpt.com/api/auth/callback"}

        def save(**kwargs):
            events.append("save")
            saved.update(kwargs)
            return 42

        def setup_twofa(*_args, **_kwargs):
            events.append("twofa")
            observe_request(tracker_box["tracker"], "https://chatgpt.com/backend-api/accounts/mfa/enroll")
            return None

        session_factory = patch.object(main, "BrowserSession", return_value=session)
        patches = [
            patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"),
            session_factory,
            patch.object(main, "close_browser_session"),
            patch.object(main, "_run_pre_otp_authorization_with_direct_fallback", side_effect=authorize),
            patch.object(main, "navigate_create_account_password", side_effect=password_page),
            patch.object(main, "generate_registration_password", return_value="ValidPassword9!"),
            patch.object(main, "request_password_sentinel_bundle", side_effect=password_bundle),
            patch.object(main, "build_sentinel_header", side_effect=sentinel_header),
            patch.object(main, "register_user", side_effect=register),
            patch.object(main, "navigate_email_otp_send", side_effect=send_otp),
            patch.object(main, "wait_for_otp", side_effect=lambda *_args, **_kwargs: events.append("otp_wait") or "123456"),
            patch.object(main, "validate_email_otp", side_effect=validate),
            patch.object(main, "navigate_about_you", side_effect=lambda *_args, **_kwargs: events.append("about_you") or "https://auth.openai.com/about-you"),
            patch.object(main, "request_sentinel_token", return_value={"token": "create-challenge"}),
            patch.object(main, "create_account", side_effect=create),
            patch.object(main, "_finalize_registration_session", return_value=({"user": {}}, "access-token")),
            patch.object(main, "setup_2fa", side_effect=setup_twofa),
            patch.object(main, "_run_optional_codex_oauth", return_value={"status": "skipped", "ok": False}),
            patch.object(main, "save_account_data", side_effect=save),
            patch.object(main, "human_delay"),
            patch.object(main._twofa_cfg, "ENABLE_2FA", True),
            patch.object(main._email_cfg, "USE_EMAIL_SERVICE", True),
            patch.object(main._protocol_cfg, "SEND_SENTINEL_ON_EMAIL_OTP_VALIDATE", False),
            patch.object(main._protocol_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", False),
            patch("core.email_provider.resolve_email_source", return_value="generic_api"),
            patch("core.flow_trigger.trigger_flow", return_value={"status": "skipped", "ok": False}),
        ]

        with ExitStack() as stack:
            for item in patches:
                active_patch = stack.enter_context(item)
                if item is session_factory:
                    create_session = active_patch
            result = main.run_registration(
                email="user@example.com",
                name="Test User",
                birthday="2000-01-01",
            )

        self.assertTrue(result["success"])
        create_session.assert_called_once()
        self.assertEqual(
            create_session.call_args.kwargs["transport_impersonate"],
            main._browser_cfg.REGISTRATION_IMPERSONATE,
        )
        self.assertIsNotNone(create_session.call_args.kwargs.get("traffic_tracker"))
        self.assertEqual(result["network_traffic"], saved["extra"]["network_traffic"])
        self.assertEqual(result["network_traffic"]["request_count"], 1)
        self.assertEqual(result["network_traffic"]["total_bytes"], 20)
        self.assertEqual(saved["extra"]["registration_password"], "ValidPassword9!")
        self.assertLess(events.index("password_page"), events.index("register_user"))
        self.assertLess(events.index("register_user"), events.index("otp_send"))
        self.assertLess(events.index("otp_send"), events.index("otp_validate"))
        self.assertLess(events.index("otp_validate"), events.index("create_account"))
        self.assertLess(events.index("create_account"), events.index("twofa"))
        self.assertLess(events.index("twofa"), events.index("save"))


if __name__ == "__main__":
    unittest.main()
