# -*- coding: utf-8 -*-
"""user/register 失败日志的安全标识提取测试。"""
import json
import unittest
from types import SimpleNamespace

from core.openai_auth import _extract_register_error_hint


def _resp(payload):
    return SimpleNamespace(content=json.dumps(payload).encode(), text="")


class RegisterErrorHintTests(unittest.TestCase):
    def test_unlisted_code_is_returned_as_bounded_identifier(self):
        self.assertEqual(
            _extract_register_error_hint(_resp({"error": {"code": "email_domain_not_allowed"}})),
            "email_domain_not_allowed",
        )

    def test_nested_and_capitalized_codes_are_normalized(self):
        self.assertEqual(
            _extract_register_error_hint(_resp({"error": {"details": [{"type": "Rate_Limited"}]}})),
            "rate_limited",
        )

    def test_free_text_is_not_logged(self):
        payload = {"detail": "Email already used for another account"}
        self.assertEqual(_extract_register_error_hint(_resp(payload)), "")

    def test_non_identifier_code_is_rejected(self):
        payload = {"error": {"code": "user@example.test"}}
        self.assertEqual(_extract_register_error_hint(_resp(payload)), "")

    def test_invalid_body_returns_empty(self):
        response = SimpleNamespace(content=b"<html>400</html>", text="")
        self.assertEqual(_extract_register_error_hint(response), "")


if __name__ == "__main__":
    unittest.main()
