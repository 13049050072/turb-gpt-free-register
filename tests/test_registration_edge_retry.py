# -*- coding: utf-8 -*-
"""注册任务 Cloudflare 边缘 403 换出口重试的测试。"""
import unittest
from unittest.mock import patch

from core import registration_service as svc
from core.proxy_utils import rotate_sticky_session

POOL_URL = "http://b96p47610-region-IN:secret@us.1024proxy.io:3000"


class EdgeBlockDetectionTests(unittest.TestCase):
    def test_http_403_is_edge_block(self):
        self.assertTrue(svc._is_edge_block_error(RuntimeError("HTTP Error 403: Forbidden")))
        self.assertTrue(svc._is_edge_block_error("HTTPError: HTTP Error 403"))

    def test_circuit_message_is_edge_block(self):
        self.assertTrue(svc._is_edge_block_error("RuntimeError: 当前 BrowserSession 已熔断冷却（剩余 900s）"))

    def test_other_errors_are_not_edge_block(self):
        self.assertFalse(svc._is_edge_block_error(RuntimeError("HTTP Error 400: bad request")))
        self.assertFalse(svc._is_edge_block_error(RuntimeError("payment declined")))


class EdgeRetryTests(unittest.TestCase):
    def test_retries_once_with_rotated_proxy(self):
        calls = []

        def call(proxy):
            calls.append(proxy)
            if proxy is None:
                raise RuntimeError("HTTP Error 403")
            return {"success": True}

        with patch.object(svc, "_edge_retry_proxy", return_value=POOL_URL) as picker:
            result = svc._run_registration_with_edge_retry(
                call, job_id=42, log=_SilentLog(),
            )
        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertEqual(calls[1], POOL_URL)
        picker.assert_called_once_with(42)

    def test_non_403_is_not_retried(self):
        calls = []

        def call(proxy):
            calls.append(proxy)
            raise RuntimeError("HTTP Error 400")

        with self.assertRaises(RuntimeError):
            svc._run_registration_with_edge_retry(call, job_id=7, log=_SilentLog())
        self.assertEqual(calls, [None])

    def test_retry_without_proxy_reraises(self):
        def call(proxy):
            raise RuntimeError("HTTP Error 403")

        with patch.object(svc, "_edge_retry_proxy", return_value=""):
            with self.assertRaises(RuntimeError):
                svc._run_registration_with_edge_retry(call, job_id=8, log=_SilentLog())


class RotateStickySessionTests(unittest.TestCase):
    def test_new_sid_replaces_old_and_keeps_credentials(self):
        rotated = rotate_sticky_session(POOL_URL, salt="job-1")
        self.assertIn("-sid-", rotated)
        self.assertIn("b96p47610-region-IN", rotated)
        self.assertIn(":secret@us.1024proxy.io:3000", rotated)
        self.assertNotEqual(rotated, POOL_URL)

    def test_existing_sid_is_replaced_not_duplicated(self):
        first = rotate_sticky_session(POOL_URL, salt="a")
        second = rotate_sticky_session(first, salt="b")
        self.assertEqual(second.count("-sid-"), 1)
        self.assertNotEqual(first, second)

    def test_non_proxy_url_untouched(self):
        self.assertEqual(rotate_sticky_session("socks5://127.0.0.1:7897"), "socks5://127.0.0.1:7897")
        self.assertEqual(rotate_sticky_session(""), "")


class _SilentLog:
    def warning(self, *args, **kwargs):
        pass


if __name__ == "__main__":
    unittest.main()
