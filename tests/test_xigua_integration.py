# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from config.env_loader import SECRET_ENV_KEYS
from core import email_provider
from webui.app import create_app
from webui.config_editor import EDITABLE_FIELDS


class XiguaConfigTests(unittest.TestCase):
    def test_email_config_declares_xigua_defaults(self):
        source = Path(email_config.__file__).read_text(encoding="utf-8")
        self.assertIn('XIGUA_API_KEY = env_str("XIGUA_API_KEY", "")', source)
        self.assertIn('XIGUA_API_BASE = "https://xigua.dasiqo.com"', source)
        self.assertIn('XIGUA_PROJECT = "openai"', source)

    def test_secret_registry_includes_xigua_api_key(self):
        self.assertIn("XIGUA_API_KEY", SECRET_ENV_KEYS)

    def test_webui_exposes_xigua_fields(self):
        fields = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(fields["XIGUA_API_BASE"]["group"], "邮箱 / OTP")
        self.assertTrue(fields["XIGUA_API_KEY"]["secret"])
        self.assertEqual(fields["XIGUA_API_KEY"]["storage"], "env")
        self.assertIn("xigua", fields["EMAIL_SOURCE"]["help"])
        template = Path("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('<option value="xigua">西瓜</option>', template)
        self.assertIn("key.startsWith('XIGUA_')", template)


class XiguaProviderTests(unittest.TestCase):
    def test_parse_sources_keeps_xigua(self):
        self.assertEqual(
            email_provider.parse_email_sources("remail,xigua,xigua"),
            ["remail", "xigua"],
        )

    @patch("core.xigua_client.pick_account")
    def test_acquire_email_uses_xigua_client(self, pick_account):
        pick_account.return_value.email = "fresh@xigua.test"
        with patch("core.email_provider.parse_email_sources", return_value=["xigua"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@xigua.test")

    @patch("core.xigua_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="xigua")
    def test_wait_for_otp_uses_xigua_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@xigua.test", after_ts=123.0), "654321")
        fetch_latest_otp.assert_called_once_with("fresh@xigua.test", after_ts=123.0)

    @patch("core.email_provider._registered_email_source", return_value=None)
    @patch("core.xigua_client.get_account_context", return_value=object())
    def test_resolve_email_source_detects_runtime_xigua_context(self, get_context, registered):
        with patch("core.remail_client.get_account_context", return_value=None):
            self.assertEqual(email_provider.resolve_email_source("fresh@xigua.test"), "xigua")

    @patch("core.xigua_client.release_account")
    @patch("core.email_provider.resolve_email_source", return_value="xigua")
    def test_release_email_uses_xigua_client(self, resolve, release_account):
        self.assertEqual(email_provider.release_email("fresh@xigua.test", status="failed"), "xigua")
        release_account.assert_called_once_with("fresh@xigua.test", status="failed", note=None)

    def test_save_account_persists_xigua_order(self):
        from core import xigua_client
        from core.account_export import save_account_data

        class _StopAfterInsert(Exception):
            pass

        xigua_client._CONTEXT_CACHE["fresh@xigua.test"] = xigua_client.XiguaAccount(
            email="fresh@xigua.test", order_id="ord_1", offer_id="off_1",
        )
        self.addCleanup(xigua_client._CONTEXT_CACHE.clear)
        # 只断言落库参数；在 insert_account 处中断，避免写批次归档等后置副作用。
        with patch("core.db.insert_account", side_effect=_StopAfterInsert) as insert_account:
            with self.assertRaises(_StopAfterInsert):
                save_account_data("fresh@xigua.test", "at", email_source="xigua")
        self.assertEqual(
            insert_account.call_args.kwargs["extra"]["email_service"],
            {"source": "xigua", "email": "fresh@xigua.test", "order_id": "ord_1", "offer_id": "off_1"},
        )


class XiguaWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_xigua_without_api_key(self, submit_registration):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "xigua"
        ), patch.object(email_config, "XIGUA_API_KEY", "", create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("西瓜 API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_xigua_config_does_not_check_local_pool(self, submit_registration, outlook_pool_summary):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "xigua"
        ), patch.object(email_config, "XIGUA_API_KEY", "xg_test", create=True), patch.object(
            email_config, "XIGUA_PROJECT", "openai", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        submit_registration.assert_called_once_with(count=1, workers=1)


if __name__ == "__main__":
    unittest.main()
