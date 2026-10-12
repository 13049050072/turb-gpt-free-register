import unittest
from unittest.mock import patch

from core import codex_oauth


class Cpa401CodexOauthTests(unittest.TestCase):
    def test_list_accepts_supported_cpa_payload_shapes_and_normalizes_email(self):
        payload = {
            "data": [
                {"provider": "codex", "name": "credential.json", "email": "User@Example.com", "status": "active"},
                {"provider": "other", "name": "other.json", "email": "other@example.com"},
            ]
        }
        with patch.object(codex_oauth, "_cpa_request_json", return_value=payload) as request_json:
            rows = codex_oauth.list_cpa_codex_auth_files()
        self.assertEqual(1, len(rows))
        self.assertEqual("user@example.com", rows[0]["email"])
        request_json.assert_called_once_with("GET", "/v0/management/auth-files")

    def test_refresh_uses_documented_read_status_operation_without_delete(self):
        with patch.object(codex_oauth, "_cpa_request_json", return_value={"results": []}) as request_json:
            result = codex_oauth.refresh_all_cpa_credentials()
        self.assertEqual({"results": []}, result)
        args, kwargs = request_json.call_args
        self.assertEqual(("POST", "/v8/management/credentials/refresh", {"all": True}), args)
        self.assertGreaterEqual(kwargs["timeout"], 300)

    def test_cpa_callback_value_is_not_written_to_logs(self):
        callback = "http://localhost:1455/auth/callback?code=secret-code&state=secret-state"
        with patch.object(codex_oauth, "_cpa_request_json", return_value={"ok": True}), patch.object(
            codex_oauth._cfg, "CPA_CALLBACK_SUBMIT_RETRIES", 1
        ), self.assertLogs(codex_oauth.logger, level="INFO") as captured:
            result = codex_oauth._submit_cpa_callback(callback)
        self.assertEqual({"ok": True}, result)
        logs = "\n".join(captured.output)
        self.assertNotIn("secret-code", logs)
        self.assertNotIn("secret-state", logs)
        self.assertIn("回调值不写日志", logs)


if __name__ == "__main__":
    unittest.main()
