import unittest
from urllib.parse import parse_qs, urlsplit

from core.sub2api_admin import Sub2ApiAdminClient, Sub2ApiAdminError


class Sub2ApiAdminClientTests(unittest.TestCase):
    def test_list_traverses_bounded_pages_and_filters_openai_oauth(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            if "page=1" in path:
                return {
                    "data": {
                        "items": [
                            {"id": "openai-1", "name": "One@Example.com", "platform": "openai", "type": "oauth"},
                            {"id": "other", "name": "other@example.com", "platform": "anthropic", "type": "oauth"},
                        ],
                        "pagination": {"current_page": 1, "total_pages": 2},
                    }
                }
            return {
                "data": {
                    "accounts": [
                        {"account_id": "openai-2", "email": "two@example.com", "platform": "openai", "type": "oauth"},
                    ],
                    "pagination": {"current_page": 2, "total_pages": 2},
                }
            }

        rows = Sub2ApiAdminClient(request).list_openai_oauth_accounts(page_size=2, max_pages=5)
        self.assertEqual(["openai-1", "openai-2"], [row["account_id"] for row in rows])
        self.assertEqual("one@example.com", rows[0]["email"])
        self.assertEqual(2, len(calls))
        self.assertTrue(all("platform=openai" in path and "type=oauth" in path and "lite=1" in path for _, path, _ in calls))
        self.assertTrue(all(body is None for _, _, body in calls))

    def test_list_honors_scalar_pagination_inside_standard_data_envelope(self):
        calls = []

        def request(method, path, body):
            calls.append(path)
            page = int(parse_qs(urlsplit(path).query)["page"][0])
            return {
                "code": 0,
                "message": "success",
                "data": {
                    "items": [{
                        "id": page - 1,
                        "email": f"page{page}@example.com",
                        "platform": "openai",
                        "type": "oauth",
                    }],
                    "page": page,
                    "page_size": 1,
                    "total": 2,
                    "pages": 2,
                }
            }

        rows = Sub2ApiAdminClient(request).list_openai_oauth_accounts(page_size=100, max_pages=5)
        self.assertEqual(["0", "1"], [row["account_id"] for row in rows])
        self.assertEqual(2, len(calls))

    def test_list_preserves_only_valid_created_at(self):
        def request(method, path, body):
            return {"data": {"items": [
                {"id": "valid", "email": "valid@example.com", "platform": "openai", "type": "oauth", "created_at": "2026-10-13T01:02:03Z", "access_token": "secret"},
                {"id": "numeric", "email": "numeric@example.com", "platform": "openai", "type": "oauth", "createdAt": 1_760_317_323},
                {"id": "invalid", "email": "invalid@example.com", "platform": "openai", "type": "oauth", "created_at": "not-a-date"},
                {"id": "future", "email": "future@example.com", "platform": "openai", "type": "oauth", "created_at": 4_102_444_800},
                {"id": "url", "email": "url@example.com", "platform": "openai", "type": "oauth", "created_at": "https://secret.example/callback"},
                {"id": "overlong", "email": "long@example.com", "platform": "openai", "type": "oauth", "created_at": "2026-10-13T01:02:03Z" + "x" * 1000},
            ]}}
        rows = Sub2ApiAdminClient(request).list_openai_oauth_accounts()
        by_id = {row["account_id"]: row for row in rows}
        self.assertEqual("2026-10-13T01:02:03+00:00", by_id["valid"]["created_at"])
        self.assertEqual("2025-10-13T01:02:03+00:00", by_id["numeric"]["created_at"])
        for account_id in ("invalid", "future", "url", "overlong"):
            self.assertEqual("", by_id[account_id]["created_at"])
        self.assertNotIn("access_token", str(rows))
        self.assertNotIn("secret.example", str(rows))

    def test_list_marks_multiple_emails_in_account_name_ambiguous(self):
        def request(method, path, body):
            return {
                "data": {
                    "items": [{
                        "id": "ambiguous",
                        "name": "First <one@example.com> Second <two@example.com>",
                        "platform": "openai",
                        "type": "oauth",
                    }],
                    "page": 1,
                    "page_size": 100,
                    "total": 1,
                }
            }

        rows = Sub2ApiAdminClient(request).list_openai_oauth_accounts()
        self.assertEqual("", rows[0]["email"])
        self.assertTrue(rows[0]["email_ambiguous"])

    def test_list_stops_on_repeated_page_without_unbounded_loop(self):
        calls = []

        def request(method, path, body):
            calls.append(path)
            return {
                "items": [{"id": "same", "email": "same@example.com", "platform": "openai", "type": "oauth"}],
                "pagination": {"has_next": True},
            }

        rows = Sub2ApiAdminClient(request).list_openai_oauth_accounts(page_size=1, max_pages=20)
        self.assertEqual(1, len(rows))
        self.assertEqual(2, len(calls))

    def test_batch_refresh_sends_ids_and_redacts_error_secrets(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            return {
                "data": {
                    "total": 2,
                    "success": 1,
                    "failed": 1,
                    "errors": [{
                        "account_id": 202,
                        "error": "401 unauthorized 'access_token': 'super-secret' Bearer eyJsecret.token callback=https://localhost/cb?code=secret",
                    }],
                    "warnings": ["redirect_uri=https://secret.example/callback"],
                }
            }

        result = Sub2ApiAdminClient(request).batch_refresh(["101", "202", "202"])
        self.assertEqual(("POST", "/api/v1/admin/accounts/batch-refresh", {"account_ids": [101, 202]}), calls[0])
        serialized = str(result)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("eyJsecret.token", serialized)
        self.assertNotIn("secret.example", serialized)
        self.assertEqual("202", result["errors"][0]["account_id"])

    def test_batch_refresh_chunks_deduped_ids_in_order_and_aggregates_bounded_results(self):
        calls = []
        error_ids = (2, 12, 25)

        def request(method, path, body):
            calls.append((method, path, body))
            call_index = len(calls) - 1
            chunk_ids = body["account_ids"]
            return {
                "data": {
                    "total": len(chunk_ids),
                    "success": len(chunk_ids) - 1,
                    "failed": 1,
                    "errors": [
                        {"account_id": error_ids[call_index], "error": f"chunk-{call_index + 1}-error"},
                        {"account_id": 999, "error": "unselected error"},
                    ],
                    "warnings": [f"chunk-{call_index + 1}-warning-{value}" for value in range(30)],
                }
            }

        account_ids = [str(value) for value in range(1, 21)] + ["5"] + [str(value) for value in range(21, 26)]
        result = Sub2ApiAdminClient(request).batch_refresh(account_ids)

        self.assertEqual([
            ("POST", "/api/v1/admin/accounts/batch-refresh", {"account_ids": list(range(1, 11))}),
            ("POST", "/api/v1/admin/accounts/batch-refresh", {"account_ids": list(range(11, 21))}),
            ("POST", "/api/v1/admin/accounts/batch-refresh", {"account_ids": list(range(21, 26))}),
        ], calls)
        self.assertEqual(25, result["total"])
        self.assertEqual(22, result["success"])
        self.assertEqual(3, result["failed"])
        self.assertEqual(["2", "12", "25"], [row["account_id"] for row in result["errors"]])
        self.assertEqual(50, len(result["warnings"]))
        self.assertEqual("chunk-1-warning-0", result["warnings"][0])
        self.assertEqual("chunk-2-warning-19", result["warnings"][-1])
        self.assertNotIn("999", str(result["errors"]))

    def test_batch_refresh_normalizes_duplicate_errors_and_inconsistent_chunk_counts(self):
        def request(method, path, body):
            first_id = body["account_ids"][0]
            if first_id == 1:
                return {
                    "data": {
                        "success": 999,
                        "failed": -3,
                        "errors": [
                            {"account_id": first_id, "error": f"first-error-{value}"}
                            for value in range(300)
                        ],
                        "warnings": [f"warning-{value}" for value in range(30)],
                    }
                }
            return {
                "data": {
                    "success": 999,
                    "failed": 999,
                    "errors": [
                        {"account_id": first_id, "error": f"second-error-{value}"}
                        for value in range(300)
                    ],
                    "warnings": [f"second-warning-{value}" for value in range(30)],
                }
            }

        result = Sub2ApiAdminClient(request).batch_refresh([str(value) for value in range(1, 21)])

        self.assertEqual(20, result["total"])
        self.assertEqual(9, result["success"])
        self.assertEqual(11, result["failed"])
        self.assertEqual(["1", "11"], [row["account_id"] for row in result["errors"]])
        self.assertEqual(2, len(result["errors"]))
        self.assertEqual(50, len(result["warnings"]))
        self.assertLessEqual(result["success"], result["total"])
        self.assertLessEqual(result["failed"], result["total"])
        self.assertLessEqual(result["success"] + result["failed"], result["total"])

    def test_batch_refresh_missing_counts_remain_conservative(self):
        def request(method, path, body):
            if body["account_ids"][0] == 1:
                return {"data": {}}
            return {"data": {"errors": [{"account_id": 11, "error": "401"}]}}

        result = Sub2ApiAdminClient(request).batch_refresh([str(value) for value in range(1, 21)])

        self.assertEqual(20, result["total"])
        self.assertEqual(0, result["success"])
        self.assertEqual(1, result["failed"])
        self.assertEqual(["11"], [row["account_id"] for row in result["errors"]])

    def test_batch_refresh_caps_unique_selection_after_duplicate_prefix(self):
        calls = []

        def request(method, path, body):
            calls.append(body["account_ids"])
            return {"data": {"success": len(body["account_ids"]), "failed": 0}}

        account_ids = ["same"] * 600 + [str(value) for value in range(1, 501)]
        result = Sub2ApiAdminClient(request).batch_refresh(account_ids)

        self.assertEqual(500, result["total"])
        self.assertEqual(50, len(calls))
        self.assertEqual(["same", 1, 2, 3, 4, 5, 6, 7, 8, 9], calls[0])
        self.assertEqual([490, 491, 492, 493, 494, 495, 496, 497, 498, 499], calls[-1])

    def test_batch_refresh_stops_after_first_chunk_exception(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            if len(calls) == 2:
                raise Sub2ApiAdminError("second chunk timed out", status_code=504, category="timeout")
            return {"data": {"total": 10, "success": 10, "failed": 0}}

        with self.assertRaises(Sub2ApiAdminError) as captured:
            Sub2ApiAdminClient(request).batch_refresh([str(value) for value in range(1, 26)])

        self.assertEqual("timeout", captured.exception.category)
        self.assertEqual([
            ("POST", "/api/v1/admin/accounts/batch-refresh", {"account_ids": list(range(1, 11))}),
            ("POST", "/api/v1/admin/accounts/batch-refresh", {"account_ids": list(range(11, 21))}),
        ], calls)

    def test_delete_deactivates_then_deletes_exact_encoded_account_id_and_discards_payloads(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            return {"data": {"access_token": "must-not-return"}}

        result = Sub2ApiAdminClient(request).delete_account("old/id")
        self.assertEqual({"ok": True, "account_id": "old/id"}, result)
        self.assertEqual([
            ("PUT", "/api/v1/admin/accounts/old%2Fid", {"status": "inactive"}),
            ("DELETE", "/api/v1/admin/accounts/old%2Fid", None),
        ], calls)
        self.assertNotIn("must-not-return", str(result))

    def test_delete_normalizes_integer_account_id_for_exact_path(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            return {}

        result = Sub2ApiAdminClient(request).delete_account(42)
        self.assertEqual({"ok": True, "account_id": "42"}, result)
        self.assertEqual([
            ("PUT", "/api/v1/admin/accounts/42", {"status": "inactive"}),
            ("DELETE", "/api/v1/admin/accounts/42", None),
        ], calls)

    def test_delete_does_not_send_delete_when_deactivation_fails(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            return {
                "code": 500,
                "message": "deactivation failed authorization=super-secret callback=https://secret.example/cb",
            }

        with self.assertRaises(Sub2ApiAdminError) as captured:
            Sub2ApiAdminClient(request).delete_account("42")

        self.assertEqual([
            ("PUT", "/api/v1/admin/accounts/42", {"status": "inactive"}),
        ], calls)
        self.assertEqual("remote", captured.exception.category)
        self.assertIn("deactivation failed", str(captured.exception))
        self.assertNotIn("super-secret", str(captured.exception))
        self.assertNotIn("secret.example", str(captured.exception))

    def test_delete_propagates_sanitized_error_after_successful_deactivation(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            if method == "DELETE":
                return {
                    "code": 500,
                    "message": "delete failed access_token=super-secret callback=https://secret.example/cb",
                }
            return {"data": {"access_token": "must-not-return"}}

        with self.assertRaises(Sub2ApiAdminError) as captured:
            Sub2ApiAdminClient(request).delete_account("42")

        self.assertEqual([
            ("PUT", "/api/v1/admin/accounts/42", {"status": "inactive"}),
            ("DELETE", "/api/v1/admin/accounts/42", None),
        ], calls)
        self.assertIn("delete failed", str(captured.exception))
        self.assertNotIn("super-secret", str(captured.exception))
        self.assertNotIn("secret.example", str(captured.exception))

    def test_nonzero_standard_envelope_is_rejected_without_leaking_message_secrets(self):
        def request(method, path, body):
            return {
                "code": 401,
                "message": "authorization=super-secret callback=https://secret.example/cb?code=secret-code",
            }

        with self.assertRaises(Sub2ApiAdminError) as captured:
            Sub2ApiAdminClient(request).list_openai_oauth_accounts()
        self.assertEqual("auth", captured.exception.category)
        self.assertNotIn("super-secret", str(captured.exception))
        self.assertNotIn("secret.example", str(captured.exception))
        self.assertNotIn("secret-code", str(captured.exception))

    def test_remote_errors_are_safe_and_classified(self):
        def request(method, path, body):
            raise RuntimeError("status=401 access_token=secret callback=https://secret")

        with self.assertRaises(Sub2ApiAdminError) as captured:
            Sub2ApiAdminClient(request).list_openai_oauth_accounts()
        self.assertEqual("auth", captured.exception.category)
        self.assertNotIn("secret", str(captured.exception).lower())


if __name__ == "__main__":
    unittest.main()
