# -*- coding: utf-8 -*-
import hashlib
import json
import unittest

from core.chatgpt2api_admin import ChatGPT2APIAdminClient, ChatGPT2APIAdminError


class ChatGPT2APIAdminTests(unittest.TestCase):
    def test_list_is_bounded_redacted_and_detects_only_explicit_invalid_signals(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            page = 1 if "page=1" in path else 2
            if page == 1:
                return {
                    "items": [
                        {
                            "id": "remote-1", "email": "One@Example.com",
                            "access_token": "old-secret", "refresh_token": "rt-secret",
                            "access_token_status": "invalid",
                        },
                        {
                            "id": "remote-2", "email": "two@example.com",
                            "status_reason": "generic upstream error",
                        },
                    ],
                    "total": 3,
                }
            return {
                "items": [{
                    "id": "remote-3", "email": "three@example.com",
                    "status_reason": "HTTP 401 bearer hidden-secret",
                }],
                "total": 3,
            }

        rows = ChatGPT2APIAdminClient(request_fn=request).list_accounts(page_size=2)
        self.assertEqual(["remote-1", "remote-2", "remote-3"], [row["remote_id"] for row in rows])
        self.assertTrue(rows[0]["invalid"])
        self.assertFalse(rows[1]["invalid"])
        self.assertTrue(rows[2]["invalid"])
        public = json.dumps(rows)
        self.assertNotIn("old-secret", public)
        self.assertNotIn("rt-secret", public)
        self.assertNotIn("hidden-secret", public)
        self.assertEqual(2, len(calls))

    def test_duplicate_remote_ids_are_preserved_for_service_conflict_detection(self):
        rows = [
            {"id": "duplicate", "email": "one@example.com", "access_token_status": "invalid"},
            {"id": "duplicate", "email": "one@example.com", "access_token_status": "invalid"},
        ]
        client = ChatGPT2APIAdminClient(request_fn=lambda _method, _path, _body: {"items": rows})
        result = client.list_accounts()
        self.assertEqual(["duplicate", "duplicate"], [item["remote_id"] for item in result])

    def test_credential_snapshot_fetches_dedicated_secret_endpoints_and_hashes_only(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            if path.endswith("/access-token"):
                return {"access_token": "access-secret"}
            if path.endswith("/refresh-token"):
                return {"refresh_token": "refresh-secret"}
            return {"items": [{"id": "remote-1", "email": "one@example.com"}]}

        client = ChatGPT2APIAdminClient(request_fn=request)
        snapshot = client.credential_snapshot("remote-1")
        self.assertEqual(hashlib.sha256(b"access-secret").hexdigest(), snapshot["at_sha256"])
        self.assertEqual({"refresh_token"}, set(snapshot["preserved_sha256"]))
        self.assertTrue(all(snapshot["preserved_sha256"].values()))
        self.assertEqual([
            ("GET", "/api/accounts/remote-1/access-token", None),
            ("GET", "/api/accounts/remote-1/refresh-token", None),
        ], calls)
        serialized = json.dumps(snapshot)
        for secret in ("access-secret", "refresh-secret"):
            self.assertNotIn(secret, serialized)

    def test_missing_refresh_token_is_explicitly_preserved(self):
        def request(method, path, body):
            if path.endswith("/access-token"):
                return {"access_token": "access-secret"}
            raise ChatGPT2APIAdminError("not found", status_code=404, category="not_found")

        snapshot = ChatGPT2APIAdminClient(request_fn=request).credential_snapshot("remote-1")
        expected = hashlib.sha256(json.dumps(
            {"present": False, "value": ""}, separators=(",", ":"), sort_keys=True
        ).encode()).hexdigest()
        self.assertEqual(expected, snapshot["preserved_sha256"]["refresh_token"])

    def test_patch_uses_exact_update_payload_and_verifies_new_at_and_preserved_rt(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            if path.endswith("/access-token"):
                return {"access_token": "new-secret"}
            if path.endswith("/refresh-token"):
                return {"refresh_token": "refresh-secret"}
            return {"data": {"updated_ids": ["acct/a b"]}, "access_token": "must-not-return"}

        client = ChatGPT2APIAdminClient(request_fn=request)
        client.credential_snapshot("acct/a b")
        result = client.patch_access_token("acct/a b", "new-secret")
        self.assertEqual(
            [
                ("GET", "/api/accounts/acct%2Fa%20b/access-token", None),
                ("GET", "/api/accounts/acct%2Fa%20b/refresh-token", None),
                ("POST", "/api/accounts/update", {"id": "acct/a b", "access_token": "new-secret"}),
                ("GET", "/api/accounts/acct%2Fa%20b/access-token", None),
                ("GET", "/api/accounts/acct%2Fa%20b/refresh-token", None),
            ], calls,
        )
        self.assertEqual({"ok": True, "remote_id": "acct/a b"}, result)
        self.assertNotIn("new-secret", json.dumps(result))

    def test_patch_rejects_post_update_access_token_mismatch(self):
        phase = {"updated": False}

        def request(method, path, body):
            if path.endswith("/access-token"):
                return {"access_token": "wrong-secret" if phase["updated"] else "old-secret"}
            if path.endswith("/refresh-token"):
                return {"refresh_token": "refresh-secret"}
            phase["updated"] = True
            return {"updated_ids": ["remote-1"]}

        client = ChatGPT2APIAdminClient(request_fn=request)
        client.credential_snapshot("remote-1")
        with self.assertRaises(ChatGPT2APIAdminError) as captured:
            client.patch_access_token("remote-1", "new-secret")
        self.assertEqual("conflict", captured.exception.category)
        self.assertEqual(409, captured.exception.status_code)

    def test_patch_rejects_post_update_refresh_token_change(self):
        phase = {"updated": False}

        def request(method, path, body):
            if path.endswith("/access-token"):
                return {"access_token": "new-secret"}
            if path.endswith("/refresh-token"):
                return {"refresh_token": "changed-refresh" if phase["updated"] else "refresh-secret"}
            phase["updated"] = True
            return {"updated_ids": ["remote-1"]}

        client = ChatGPT2APIAdminClient(request_fn=request)
        client.credential_snapshot("remote-1")
        with self.assertRaises(ChatGPT2APIAdminError) as captured:
            client.patch_access_token("remote-1", "new-secret")
        self.assertEqual("conflict", captured.exception.category)
        self.assertEqual(409, captured.exception.status_code)

    def test_patch_rejects_unconfirmed_or_different_updated_id(self):
        for payload in ({}, {"updated_ids": ["other-id"]}):
            def request(method, path, body, result=payload):
                if path.endswith("/access-token"):
                    return {"access_token": "old-secret"}
                if path.endswith("/refresh-token"):
                    raise ChatGPT2APIAdminError("not found", status_code=404, category="not_found")
                return result

            with self.subTest(payload=payload), self.assertRaises(ChatGPT2APIAdminError) as captured:
                ChatGPT2APIAdminClient(request_fn=request).patch_access_token("remote-1", "new-secret")
            self.assertEqual("conflict", captured.exception.category)
            self.assertEqual(409, captured.exception.status_code)

    def test_request_failures_do_not_echo_credentials(self):
        def request(_method, _path, _body):
            raise RuntimeError("access_token=secret callback=https://example.test/cb?code=secret")

        with self.assertRaises(ChatGPT2APIAdminError) as captured:
            ChatGPT2APIAdminClient(request_fn=request).list_accounts()
        self.assertEqual("ChatGPT2API 管理接口请求失败", str(captured.exception))
        self.assertNotIn("secret", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
