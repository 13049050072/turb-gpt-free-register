# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import Mock, patch

import requests

from config import email as email_config
from core import xigua_client


def _response(status_code: int, payload) -> Mock:
    response = Mock(status_code=status_code)
    response.json.return_value = payload
    response.text = json.dumps(payload, ensure_ascii=False)
    return response


PROJECTS = [
    {
        "id": "prj_github",
        "slug": "github",
        "name": "GitHub",
        "enabled": True,
        "offers": [{"id": "off_github", "mailbox_type": "iCloud", "price_cents": 2, "enabled": True, "stock": 10}],
    },
    {
        "id": "prj_openai",
        "slug": "openai",
        "name": "OpenAI",
        "enabled": True,
        "offers": [
            {"id": "off_empty", "mailbox_type": "iCloud", "price_cents": 1, "enabled": True, "stock": 0},
            {"id": "off_outlook", "mailbox_type": "Outlook", "price_cents": 2, "enabled": True, "stock": 5},
            {"id": "off_icloud", "mailbox_type": "iCloud", "price_cents": 3, "enabled": True, "stock": 4793},
        ],
    },
]


class XiguaClientTests(unittest.TestCase):
    def setUp(self):
        xigua_client._CONTEXT_CACHE.clear()
        self.config = patch.multiple(
            email_config,
            create=True,
            XIGUA_API_BASE="https://xigua.dasiqo.com/#docs",
            XIGUA_API_KEY="xg_test",
            XIGUA_PROJECT="openai",
            XIGUA_MAILBOX_TYPE="",
            XIGUA_OFFER_ID="",
        )
        self.config.start()
        self.addCleanup(self.config.stop)

    def test_base_url_accepts_docs_link(self):
        self.assertEqual(xigua_client._base_url("https://xigua.dasiqo.com/#docs"), "https://xigua.dasiqo.com")
        self.assertEqual(xigua_client._base_url("xigua.dasiqo.com/api/v1/"), "https://xigua.dasiqo.com")

    @patch("core.xigua_client.requests.request")
    def test_pick_account_selects_cheapest_in_stock_offer_and_creates_order(self, request):
        request.side_effect = [
            _response(200, PROJECTS),
            _response(201, {
                "id": "ord_1",
                "email": "fresh@icloud.test",
                "state": "waiting",
                "price_cents": 2,
                "repeat_free": False,
            }),
        ]

        account = xigua_client.pick_account()

        self.assertEqual(account.email, "fresh@icloud.test")
        self.assertEqual(account.order_id, "ord_1")
        self.assertEqual(account.offer_id, "off_outlook")
        self.assertIs(xigua_client.get_account_context("FRESH@ICLOUD.TEST"), account)
        list_call, create_call = request.call_args_list
        self.assertEqual(list_call.args[:2], ("GET", "https://xigua.dasiqo.com/api/v1/projects"))
        self.assertEqual(list_call.kwargs["headers"]["Authorization"], "Bearer xg_test")
        self.assertEqual(create_call.args[:2], ("POST", "https://xigua.dasiqo.com/api/v1/orders"))
        self.assertEqual(create_call.kwargs["json"], {"offer_id": "off_outlook"})
        self.assertTrue(create_call.kwargs["headers"]["Idempotency-Key"].startswith("turb-gpt-free-register-"))

    @patch("core.xigua_client.requests.request")
    def test_mailbox_type_filter_limits_offers(self, request):
        request.side_effect = [
            _response(200, PROJECTS),
            _response(201, {"id": "ord_1", "email": "fresh@icloud.test", "state": "waiting"}),
        ]
        with patch.object(email_config, "XIGUA_MAILBOX_TYPE", "icloud"):
            account = xigua_client.pick_account()
        self.assertEqual(account.offer_id, "off_icloud")

    @patch("core.xigua_client.requests.request")
    def test_configured_offer_id_skips_project_lookup(self, request):
        request.return_value = _response(201, {"id": "ord_1", "email": "fresh@icloud.test", "state": "waiting"})
        with patch.object(email_config, "XIGUA_OFFER_ID", "off_fixed"):
            xigua_client.pick_account()
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["json"], {"offer_id": "off_fixed"})

    @patch("core.xigua_client.time.sleep")
    @patch("core.xigua_client.requests.request")
    def test_create_order_retries_with_same_idempotency_key(self, request, sleep):
        request.side_effect = [
            requests.ConnectionError("reset"),
            _response(201, {"id": "ord_1", "email": "fresh@icloud.test", "state": "waiting"}),
        ]
        with patch.object(email_config, "XIGUA_OFFER_ID", "off_fixed"):
            xigua_client.pick_account()
        first, second = request.call_args_list
        self.assertEqual(
            first.kwargs["headers"]["Idempotency-Key"],
            second.kwargs["headers"]["Idempotency-Key"],
        )

    @patch("core.xigua_client.requests.request")
    def test_unknown_project_reports_available_projects(self, request):
        request.return_value = _response(200, PROJECTS)
        with patch.object(email_config, "XIGUA_PROJECT", "claude"):
            with self.assertRaisesRegex(xigua_client.XiguaError, "github, openai"):
                xigua_client.pick_account()

    @patch("core.xigua_client.time.sleep")
    @patch("core.xigua_client.requests.request")
    def test_fetch_latest_otp_polls_until_order_succeeds(self, request, sleep):
        xigua_client._CONTEXT_CACHE["fresh@icloud.test"] = xigua_client.XiguaAccount(
            email="fresh@icloud.test", order_id="ord_1", offer_id="off_icloud",
        )
        request.side_effect = [
            _response(200, {"id": "ord_1", "email": "fresh@icloud.test", "state": "waiting"}),
            _response(200, {"id": "ord_1", "email": "fresh@icloud.test", "state": "succeeded", "code": "654321"}),
        ]

        code = xigua_client.fetch_latest_otp("fresh@icloud.test", after_ts=0.0, max_wait=30, poll_interval=1)

        self.assertEqual(code, "654321")
        self.assertEqual(request.call_args.args[:2], ("GET", "https://xigua.dasiqo.com/api/v1/orders/ord_1"))
        # 文档建议 5–10 秒查询一次，配置更小的间隔也不低于 5 秒。
        sleep.assert_called_once_with(5)
        self.assertTrue(xigua_client.get_account_context("fresh@icloud.test").consumed)

    @patch("core.xigua_client.time.sleep")
    @patch("core.xigua_client.requests.request")
    def test_second_otp_uses_repeat_order_instead_of_returning_old_code(self, request, sleep):
        xigua_client._CONTEXT_CACHE["fresh@icloud.test"] = xigua_client.XiguaAccount(
            email="fresh@icloud.test", order_id="ord_1", offer_id="off_icloud", consumed=True,
        )
        request.side_effect = [
            _response(200, {"id": "ord_1", "email": "fresh@icloud.test", "state": "succeeded", "code": "654321"}),
            _response(201, {"id": "ord_2", "email": "fresh@icloud.test", "state": "waiting", "price_cents": 0}),
            _response(200, {"id": "ord_2", "email": "fresh@icloud.test", "state": "succeeded", "code": "112233"}),
        ]

        code = xigua_client.fetch_latest_otp("fresh@icloud.test", after_ts=None, max_wait=30)

        self.assertEqual(code, "112233")
        repeat_call = request.call_args_list[1]
        self.assertEqual(
            repeat_call.args[:2],
            ("POST", "https://xigua.dasiqo.com/api/v1/orders/ord_1/repeat"),
        )
        self.assertEqual(repeat_call.kwargs["json"], {})
        self.assertIn("Idempotency-Key", repeat_call.kwargs["headers"])
        self.assertEqual(xigua_client.get_account_context("fresh@icloud.test").order_id, "ord_2")

    @patch("core.xigua_client.time.sleep")
    @patch("core.xigua_client.requests.request")
    def test_expired_order_is_repeated(self, request, sleep):
        xigua_client._CONTEXT_CACHE["fresh@icloud.test"] = xigua_client.XiguaAccount(
            email="fresh@icloud.test", order_id="ord_1",
        )
        request.side_effect = [
            _response(200, {"id": "ord_1", "email": "fresh@icloud.test", "state": "expired"}),
            _response(201, {"id": "ord_2", "email": "fresh@icloud.test", "state": "waiting"}),
            _response(200, {"id": "ord_2", "email": "fresh@icloud.test", "state": "succeeded", "code": "112233"}),
        ]
        self.assertEqual(xigua_client.fetch_latest_otp("fresh@icloud.test", max_wait=30), "112233")

    @patch("core.xigua_client.time.sleep")
    @patch("core.xigua_client.requests.request")
    def test_repeat_conflict_adopts_existing_waiting_order(self, request, sleep):
        xigua_client._CONTEXT_CACHE["fresh@icloud.test"] = xigua_client.XiguaAccount(
            email="fresh@icloud.test", order_id="ord_1", consumed=True,
        )
        request.side_effect = [
            _response(200, {"id": "ord_1", "email": "fresh@icloud.test", "state": "succeeded", "code": "654321"}),
            _response(409, {"error": {"code": "order_waiting", "message": "已有等待订单"}}),
            _response(200, [
                {"id": "ord_other", "email": "other@icloud.test", "state": "waiting", "created_at": "2026-10-15T01:00:00Z"},
                {"id": "ord_3", "email": "FRESH@icloud.test", "state": "waiting", "created_at": "2026-10-15T01:00:00Z"},
            ]),
            _response(200, {"id": "ord_3", "email": "fresh@icloud.test", "state": "succeeded", "code": "778899"}),
        ]
        self.assertEqual(xigua_client.fetch_latest_otp("fresh@icloud.test", max_wait=30), "778899")
        self.assertEqual(request.call_args_list[2].args[:2], ("GET", "https://xigua.dasiqo.com/api/v1/orders"))

    @patch("core.xigua_client.time.sleep")
    @patch("core.xigua_client.requests.request")
    def test_invalid_key_fails_fast(self, request, sleep):
        xigua_client._CONTEXT_CACHE["fresh@icloud.test"] = xigua_client.XiguaAccount(
            email="fresh@icloud.test", order_id="ord_1",
        )
        request.return_value = _response(401, {"error": {"code": "unauthorized", "message": "请先登录或检查 API 密钥"}})
        with self.assertRaisesRegex(xigua_client.XiguaError, "API Key 无效"):
            xigua_client.fetch_latest_otp("fresh@icloud.test", max_wait=30)
        request.assert_called_once()

    @patch("core.xigua_client._saved_context_metadata")
    @patch("core.xigua_client.time.sleep")
    @patch("core.xigua_client.requests.request")
    def test_restored_order_after_restart_is_repeated(self, request, sleep, saved):
        saved.return_value = {
            "source": "xigua",
            "email": "fresh@icloud.test",
            "order_id": "ord_saved",
            "offer_id": "off_icloud",
        }
        request.side_effect = [
            _response(200, {"id": "ord_saved", "email": "fresh@icloud.test", "state": "succeeded", "code": "654321"}),
            _response(201, {"id": "ord_new", "email": "fresh@icloud.test", "state": "waiting", "price_cents": 0}),
            _response(200, {"id": "ord_new", "email": "fresh@icloud.test", "state": "succeeded", "code": "445566"}),
        ]
        self.assertEqual(xigua_client.fetch_latest_otp("FRESH@ICLOUD.TEST", max_wait=30), "445566")
        self.assertEqual(
            request.call_args_list[1].args[:2],
            ("POST", "https://xigua.dasiqo.com/api/v1/orders/ord_saved/repeat"),
        )

    @patch("core.xigua_client._saved_context_metadata", return_value={})
    @patch("core.xigua_client.requests.request")
    def test_restore_searches_recent_orders_by_exact_email(self, request, saved):
        request.return_value = _response(200, {"orders": [
            {"id": "ord_old", "email": "fresh@icloud.test", "state": "succeeded", "created_at": "2026-10-14T01:00:00Z"},
            {"id": "ord_new", "email": "fresh@icloud.test", "state": "succeeded", "created_at": "2026-10-15T01:00:00Z"},
            {"id": "ord_x", "email": "xfresh@icloud.test", "state": "waiting", "created_at": "2026-10-16T01:00:00Z"},
        ]})
        account = xigua_client.restore_account_context("fresh@icloud.test")
        self.assertEqual(account.order_id, "ord_new")
        self.assertTrue(account.consumed)

    @patch("core.xigua_client.requests.request")
    def test_release_cancels_waiting_order(self, request):
        xigua_client._CONTEXT_CACHE["fresh@icloud.test"] = xigua_client.XiguaAccount(
            email="fresh@icloud.test", order_id="ord_1",
        )
        request.side_effect = [
            _response(200, {"id": "ord_1", "email": "fresh@icloud.test", "state": "waiting"}),
            _response(200, {"id": "ord_1", "email": "fresh@icloud.test", "state": "cancelled"}),
        ]
        xigua_client.release_account("fresh@icloud.test", status="available")
        self.assertEqual(
            request.call_args.args[:2],
            ("POST", "https://xigua.dasiqo.com/api/v1/orders/ord_1/cancel"),
        )
        self.assertIsNone(xigua_client.get_account_context("fresh@icloud.test"))

    @patch("core.xigua_client.requests.request")
    def test_release_keeps_finished_order(self, request):
        xigua_client._CONTEXT_CACHE["fresh@icloud.test"] = xigua_client.XiguaAccount(
            email="fresh@icloud.test", order_id="ord_1", consumed=True,
        )
        request.return_value = _response(200, {"id": "ord_1", "email": "fresh@icloud.test", "state": "succeeded", "code": "1"})
        xigua_client.release_account("fresh@icloud.test", status="failed")
        request.assert_called_once()

    def test_context_metadata_is_persistable(self):
        xigua_client._CONTEXT_CACHE["fresh@icloud.test"] = xigua_client.XiguaAccount(
            email="fresh@icloud.test", order_id="ord_1", offer_id="off_icloud",
        )
        self.assertEqual(
            xigua_client.get_account_context_metadata("fresh@icloud.test"),
            {"source": "xigua", "email": "fresh@icloud.test", "order_id": "ord_1", "offer_id": "off_icloud"},
        )


if __name__ == "__main__":
    unittest.main()
