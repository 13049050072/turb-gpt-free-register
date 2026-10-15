# -*- coding: utf-8 -*-
import unittest

from core.openai_auth import detect_account_unusable_response_body, detect_account_unusable_text
from core.browser_use_codex_oauth import _wait_after_email_submit


class CodexDeadAccountDetectionTests(unittest.TestCase):
    def test_detect_account_unusable_response_body_uses_error_code(self):
        self.assertEqual(
            detect_account_unusable_response_body('{"error":{"code":"account_deactivated"}}'),
            "account_deactivated",
        )
        self.assertEqual(
            detect_account_unusable_response_body('{"error":{"code":"account_deleted"}}'),
            "account_deleted",
        )
        self.assertEqual(detect_account_unusable_response_body('Your account has been deactivated.'), "")

    def test_codex_mfa_403_deleted_or_deactivated_text_is_dead_account(self):
        text = (
            'RuntimeError: [Codex] MFA 验证失败 status=403: { "error": { "message": "You do not have an account '
            'because it has been deleted or deactivated. If you believe this was an error, please contact us'
        )
        self.assertTrue(detect_account_unusable_text(text))
        self.assertEqual("", detect_account_unusable_text("RuntimeError: [Codex] MFA 验证失败 status=401: invalid code"))

    def test_run_codex_oauth_maps_dead_account_error_from_any_stage_to_deactivated(self):
        from unittest.mock import patch

        import core.codex_oauth as codex_oauth

        error = RuntimeError(
            '[Codex] MFA 验证失败 status=403: { "error": { "message": "You do not have an account because it '
            'has been deleted or deactivated."'
        )
        from unittest.mock import MagicMock

        from config import codex as codex_cfg

        with patch.object(codex_cfg, "CODEX_OAUTH_DRIVER", "protocol", create=True), patch.object(
            codex_oauth, "_resolve_codex_proxy", return_value=None
        ), patch.object(codex_oauth, "BrowserSession", MagicMock()), patch.object(
            codex_oauth, "_codex_auth_url_source", side_effect=error
        ):
            result = codex_oauth.run_codex_oauth("dead@example.com", otp_provider=lambda *_: "", force=True)
        self.assertEqual("deactivated", result["status"])
        self.assertFalse(result["ok"])

    def test_browser_use_email_submit_returns_deactivated_from_response_tracker(self):
        class Body:
            def inner_text(self, timeout=1000):
                return "Your account has been deactivated."

        class Page:
            url = "https://auth.openai.com/email-verification"
            def locator(self, selector):
                return Body()

        tracker = {"code": "account_deactivated"}
        self.assertEqual(_wait_after_email_submit(Page(), timeout=1, dead_tracker=tracker), "deactivated:account_deactivated")
        self.assertNotEqual(_wait_after_email_submit(Page(), timeout=1, dead_tracker={}), "deactivated:account_deactivated")


if __name__ == "__main__":
    unittest.main()
