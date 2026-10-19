import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from core import db
from core.chatgpt_plan import (
    PAYMENT_CHECKOUT_PATH,
    check_account_plan,
    check_payment_method_availability,
    parse_oaics_payment_response,
    parse_payment_method_response,
    parse_stripe_payment_response,
)
from webui.app import _compact_account_for_list


class _Response:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text
        self.headers = {}

    def json(self):
        if isinstance(self._data, BaseException):
            raise self._data
        return self._data


class _Session:
    def __init__(self, response=None, error=None, plan_data=None):
        self.response = response
        self.error = error
        self.plan_data = plan_data
        self.posts = []
        self.gets = []
        self.proxy = ""
        self.device_id = "device-test"
        self.oai_session_id = "session-test"
        self.exit_geo = {"country": "US"}
        self.browser_profile = {"geo": {"country": "US"}, "navigator_language": "en-US"}
        self.blocked_until = 0
        self.blocked_reason = ""

    def get_chatgpt_headers(self, **_kwargs):
        return {"oai-device-id": self.device_id, "oai-session-id": self.oai_session_id}

    def get(self, url, **_kwargs):
        self.gets.append(url)
        return _Response(data=self.plan_data)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    def js_timezone_offset_min(self):
        return -480

    def fingerprint_summary_text(self):
        return "test-fingerprint"


class _CheckoutContractSession(_Session):
    def __init__(self):
        super().__init__()
        self.contract_valid = False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        headers = kwargs.get("headers", {})
        self.contract_valid = (
            url == "https://chatgpt.com" + PAYMENT_CHECKOUT_PATH
            and headers.get("x-openai-target-path") == PAYMENT_CHECKOUT_PATH
            and headers.get("x-openai-target-route") == PAYMENT_CHECKOUT_PATH
            and kwargs.get("json") == {
                "entry_point": "all_plans_pricing_modal",
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": "US", "currency": "USD"},
                "checkout_ui_mode": "custom",
                "promo_campaign": {
                    "promo_campaign_id": "campaign-1",
                    "is_coupon_from_query_param": False,
                },
            }
        )
        if not self.contract_valid:
            return _Response(400, data={"error": "invalid checkout request"})
        return _Response(200, data={"payment_method_types": ["card"]})


class PaymentMethodParserTests(unittest.TestCase):
    def test_oaics_shape_is_normalized_and_bounded(self):
        result = parse_oaics_payment_response({
            "paymentMethodTypes": [{"type": "card"}, {"code": "mystery-provider"}],
            "payment_session": {"type": "checkout.session"},
            "total_summary": {"due": 0, "currency": "usd"},
            "checkout_url": "https://provider.invalid/session/secret",
            "session_id": "session-secret",
            "client_secret": "client-secret",
        })
        self.assertEqual(result["payment_method_check_status"], "checked")
        self.assertEqual(result["payment_method_types"], ["card", "mystery_provider"])
        self.assertEqual(result["payment_method_labels"], ["银行卡", "其他支付方式"])
        self.assertEqual(result["payment_session_type"], "checkout")
        self.assertEqual(result["payment_amount"], 0)
        self.assertEqual(result["payment_currency"], "USD")
        self.assertNotIn("checkout_url", json.dumps(result))
        self.assertNotIn("session-secret", json.dumps(result))
        self.assertNotIn("client-secret", json.dumps(result))

    def test_stripe_payment_page_shape_is_supported(self):
        result = parse_stripe_payment_response({
            "object": "checkout.session",
            "payment_method_types": ["card", "link", "apple_pay"],
            "invoice": {"amount_due": 499, "currency": "eur"},
            "mode": "subscription",
            "client_secret": "secret-not-returned",
        })
        self.assertEqual(result["payment_method_types"], ["card", "link", "apple_pay"])
        self.assertEqual(result["payment_method_labels"], ["银行卡", "Link", "Apple Pay"])
        self.assertEqual(result["payment_session_type"], "subscription")
        self.assertEqual(result["payment_amount"], 499)
        self.assertEqual(result["payment_currency"], "EUR")
        self.assertNotIn("secret-not-returned", json.dumps(result))

    def test_unknown_method_is_retained_with_generic_label(self):
        result = parse_payment_method_response({
            "payment_method_types": ["pm-custom/provider", "card", "card"],
        })
        self.assertEqual(result["payment_method_types"], ["pm_custom_provider", "card"])
        self.assertEqual(result["payment_method_labels"], ["其他支付方式", "银行卡"])

    def test_malformed_and_missing_method_fields_raise_parser_error(self):
        for value in (None, [], {}, {"currency": "USD"}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_payment_method_response(value)


class PaymentMethodFlowTests(unittest.TestCase):
    @staticmethod
    def eligible_plan(**overrides):
        result = {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": True,
            "plus_trial_discount_percentage": 100,
            "plus_trial_duration_num_periods": 1,
            "plus_trial_duration_period": "month",
            "eligible_promo_campaigns": {"plus": {"id": "campaign-1", "metadata": {"plan_name": "chatgptplusplan"}}},
            "eligible_offer_ids": ["chatgptplusplan"],
        }
        result.update(overrides)
        return result

    def test_exact_one_month_free_trial_runs_checkout(self):
        session = _Session(response=_Response(200, data={"payment_method_types": ["card"]}))
        result = check_payment_method_availability(session, "token", self.eligible_plan())
        self.assertEqual(result["payment_method_check_status"], "checked")
        self.assertEqual(len(session.posts), 1)

    def test_malformed_promotion_identifier_skips_checkout(self):
        plan = self.eligible_plan(
            eligible_promo_campaigns={"plus": {"metadata": {"plan_name": "chatgptplusplan"}}},
        )
        session = _Session(response=_Response(200, data={"payment_method_types": ["card"]}))
        result = check_payment_method_availability(session, "token", plan)
        self.assertEqual(result["payment_method_check_status"], "not_applicable")
        self.assertEqual(session.posts, [])

    def test_checkout_region_missing_is_best_effort_error_without_request(self):
        session = _Session(response=_Response(200, data={"payment_method_types": ["card"]}))
        session.exit_geo = {}
        session.browser_profile = {}
        result = check_payment_method_availability(session, "token", self.eligible_plan())
        self.assertEqual(result["payment_method_check_status"], "error")
        self.assertEqual(result["payment_method_error"], "payment region unavailable")
        self.assertEqual(session.posts, [])

    def test_no_promotion_skips_checkout(self):
        session = _Session(response=_Response(200, data={"payment_method_types": ["card"]}))
        result = check_payment_method_availability(
            session, "token", self.eligible_plan(eligible_promo_campaigns={}),
        )
        self.assertEqual(result["payment_method_check_status"], "not_applicable")
        self.assertEqual(session.posts, [])

    def test_paid_or_non_free_plan_skips_checkout(self):
        session = _Session(response=_Response(200, data={"payment_method_types": ["card"]}))
        result = check_payment_method_availability(
            session, "token", self.eligible_plan(current_plan_type="plus"),
        )
        self.assertEqual(result["payment_method_check_status"], "not_applicable")
        self.assertEqual(session.posts, [])

    def test_zero_or_other_trial_duration_skips_checkout(self):
        for periods, period in ((0, "month"), (2, "month"), (1, "week")):
            with self.subTest(periods=periods, period=period):
                session = _Session(response=_Response(200, data={"payment_method_types": ["card"]}))
                result = check_payment_method_availability(
                    session,
                    "token",
                    self.eligible_plan(
                        plus_trial_duration_num_periods=periods,
                        plus_trial_duration_period=period,
                    ),
                )
                self.assertEqual(result["payment_method_check_status"], "not_applicable")
                self.assertEqual(session.posts, [])

    def test_unknown_or_malformed_trial_duration_skips_checkout(self):
        for periods, period in ((None, "month"), ("1", "month"), (1, None), (1, {"unit": "month"})):
            with self.subTest(periods=periods, period=period):
                session = _Session(response=_Response(200, data={"payment_method_types": ["card"]}))
                result = check_payment_method_availability(
                    session,
                    "token",
                    self.eligible_plan(
                        plus_trial_duration_num_periods=periods,
                        plus_trial_duration_period=period,
                    ),
                )
                self.assertEqual(result["payment_method_check_status"], "not_applicable")
                self.assertEqual(session.posts, [])

    def test_applicable_offer_without_session_is_best_effort_error(self):
        result = check_payment_method_availability(None, "token", self.eligible_plan())
        self.assertEqual(result["payment_method_check_status"], "error")
        self.assertEqual(result["payment_method_error"], "payment transport error")

    def test_http_transport_malformed_and_missing_fields_are_best_effort(self):
        cases = [
            _Session(response=_Response(429, data={"payment_method_types": ["card"]})),
            _Session(error=RuntimeError("https://provider.invalid/?client_secret=secret")),
            _Session(response=_Response(200, data=ValueError("response body"))),
            _Session(response=_Response(200, data={"currency": "USD"})),
        ]
        for session in cases:
            with self.subTest(session=session):
                result = check_payment_method_availability(session, "token", self.eligible_plan())
                self.assertEqual(result["payment_method_check_status"], "error")
                self.assertTrue(result["payment_method_error"])
                self.assertNotIn("provider.invalid", json.dumps(result))
                self.assertNotIn("client_secret", json.dumps(result))

    def test_known_checkout_contract_avoids_http_400(self):
        session = _CheckoutContractSession()
        result = check_payment_method_availability(session, "token", self.eligible_plan())
        self.assertEqual(result["payment_method_check_status"], "checked")
        self.assertTrue(session.contract_valid)
        self.assertEqual(len(session.posts), 1)

    def test_success_uses_current_session_and_only_bounded_checkout_body(self):
        session = _Session(response=_Response(200, data={
            "payment_method_types": ["card"], "session_type": "checkout", "amount": 0, "currency": "usd",
            "url": "https://provider.invalid/secret", "session_id": "secret-id",
        }))
        result = check_payment_method_availability(session, "token", self.eligible_plan())
        self.assertEqual(result["payment_method_check_status"], "checked")
        self.assertEqual(session.posts[0][0], "https://chatgpt.com" + PAYMENT_CHECKOUT_PATH)
        body = session.posts[0][1]["json"]
        self.assertEqual(body, {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "US", "currency": "USD"},
            "checkout_ui_mode": "custom",
            "promo_campaign": {
                "promo_campaign_id": "campaign-1",
                "is_coupon_from_query_param": False,
            },
        })
        self.assertNotIn("token", json.dumps(body))
        self.assertNotIn("provider.invalid", json.dumps(result))
        self.assertNotIn("secret-id", json.dumps(result))

    def test_payment_failure_does_not_fail_successful_plan_check(self):
        session = _Session(
            response=_Response(503, data={"secret": "not-returned"}),
            plan_data={"accounts": {"default": {
                "account": {"account_id": "account-1", "plan_type": "free"},
                "eligible_promo_campaigns": {"plus": {"id": "campaign-1", "metadata": {
                    "plan_name": "chatgptplusplan",
                    "discount": {"percentage": 100},
                    "duration": {"num_periods": 1, "period": "month"},
                }}},
                "eligible_offers": {"offers": [{"id": "chatgptplusplan"}]},
            }}},
        )
        # The plan GET and checkout POST need different deterministic responses.
        with patch("core.chatgpt_plan.token_claims", return_value={
            "payload": {}, "email": "account@example.test", "account_id": "account-1",
            "token_expired": False, "claim_plan_type": "free",
        }), patch("core.chatgpt_plan._clear_plan_circuit"):
            # Use an explicit session so no BrowserSession or network is created.
            result = check_account_plan("token", session=session, max_attempts=1, timezone_offset_min="-540")
        self.assertTrue(result["ok"])
        self.assertEqual(result["payment_method_check_status"], "error")
        self.assertEqual(result["payment_method_error"], "HTTP 503")
        self.assertEqual(len(session.posts), 1)


class PaymentMethodPersistenceAndTemplateTests(unittest.TestCase):
    def test_persistence_and_api_projection_are_allowlisted(self):
        row = {"id": 1, "email": "account@example.test"}
        result = {
            "ok": True,
            "current_plan_type": "free",
            "payment_method_check_status": "checked",
            "payment_method_checked_at": "2026-10-19T12:00:00",
            "payment_method_error": None,
            "payment_method_types": ["card", "unknown_type"],
            "payment_method_labels": ["银行卡", "其他支付方式"],
            "payment_session_type": "checkout",
            "payment_amount": 0,
            "payment_currency": "USD",
            "checkout_url": "https://provider.invalid/secret",
            "client_secret": "do-not-store",
            "session_id": "do-not-store",
        }

        @contextmanager
        def account_for_update(*_args, **_kwargs):
            yield row

        with patch.object(db, "_account_for_update", side_effect=account_for_update):
            self.assertTrue(db.update_account_plan_check(acc_id=1, result=result))
        self.assertEqual(row["payment_method_types"], ["card", "unknown_type"])
        self.assertNotIn("checkout_url", row)
        self.assertNotIn("client_secret", row)
        compact = _compact_account_for_list(row)
        self.assertEqual(compact["payment_method_labels"], ["银行卡", "其他支付方式"])
        self.assertNotIn("checkout_url", json.dumps(compact))

        with patch.object(db, "_query_collection_page", return_value=([row], 1, "")):
            projection = db.list_account_plan_check_statuses()
        item = projection["items"][0]
        self.assertEqual(item["payment_method_check_status"], "checked")
        self.assertEqual(item["payment_currency"], "USD")
        self.assertNotIn("client_secret", json.dumps(projection))

    def test_both_account_templates_render_all_payment_states(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("index.html", "index_legacy.html"):
            page = (root / "webui" / "templates" / name).read_text(encoding="utf-8")
            with self.subTest(template=name):
                self.assertIn("function _paymentMethodCell(r)", page)
                self.assertIn("payment_method_check_status", page)
                self.assertIn("支付方式: 检查失败", page)
                self.assertIn("支付方式: 不适用", page)
                self.assertIn("支付方式: 未检查", page)
                self.assertIn("payment_method_labels", page)


if __name__ == "__main__":
    unittest.main()
