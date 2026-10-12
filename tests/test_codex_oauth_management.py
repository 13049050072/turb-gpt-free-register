import unittest
from unittest.mock import patch

from core import codex_oauth


class CodexOauthManagementTests(unittest.TestCase):
    def test_existing_cpa_list_compatibility_is_preserved(self):
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

    def test_existing_cpa_refresh_compatibility_is_preserved(self):
        with patch.object(codex_oauth, "_cpa_request_json", return_value={"results": []}) as request_json:
            result = codex_oauth.refresh_all_cpa_credentials()
        self.assertEqual({"results": []}, result)
        args, kwargs = request_json.call_args
        self.assertEqual(("POST", "/v8/management/credentials/refresh", {"all": True}), args)
        self.assertGreaterEqual(kwargs["timeout"], 300)

    def test_sub2_callback_and_response_secrets_are_not_written_to_logs(self):
        callback = "http://localhost:1455/auth/callback?code=secret-code&state=secret-state"
        response = {"data": {"id": "account-1", "access_token": "secret-token"}}
        with patch.object(codex_oauth, "_sub2_codex_request_json", return_value=response), patch.object(
            codex_oauth._cfg, "CPA_CALLBACK_SUBMIT_RETRIES", 1
        ), self.assertLogs(codex_oauth.logger, level="INFO") as captured:
            result = codex_oauth._submit_sub2_callback(callback, session_id="session-1")
        self.assertEqual(response, result)
        logs = "\n".join(captured.output)
        self.assertNotIn("secret-code", logs)
        self.assertNotIn("secret-state", logs)
        self.assertNotIn("secret-token", logs)
        self.assertNotIn(callback, logs)
        self.assertIn("callback 内容不写日志", logs)

    def test_codex_result_does_not_return_callback_or_secret_message_content(self):
        result = codex_oauth._codex_result(
            status="failed",
            callback_url="http://localhost:1455/auth/callback?code=secret-code&state=secret-state",
            message="access_token=secret-token callback=https://secret.example/cb?code=secret-code",
        )
        serialized = str(result)
        self.assertIsNone(result["callback_url"])
        self.assertNotIn("secret-code", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("secret.example", serialized)

    def test_sub2_authorize_url_is_not_written_to_logs(self):
        payload = {
            "data": {
                "auth_url": "https://auth.example/authorize?state=secret-state",
                "state": "secret-state",
                "session_id": "session-1",
            }
        }
        with patch.object(codex_oauth, "_sub2_codex_request_json", return_value=payload), patch.object(
            codex_oauth, "_sub2_codex_base", return_value="https://sub2.example"
        ), self.assertLogs(codex_oauth.logger, level="INFO") as captured:
            result = codex_oauth._request_sub2_authorize_url()
        self.assertEqual(payload["data"]["auth_url"], result["auth_url"])
        logs = "\n".join(captured.output)
        self.assertNotIn("secret-state", logs)
        self.assertNotIn("auth.example", logs)
        self.assertIn("不写日志", logs)


if __name__ == "__main__":
    unittest.main()
