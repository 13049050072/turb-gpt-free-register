# -*- coding: utf-8 -*-
import json
import logging
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import core.protocol_traffic as traffic_module
import core.session as session_module
from core.protocol_traffic import ProtocolTrafficTracker, safe_protocol_url
from core.session import BrowserSession


class ProtocolTrafficTrackerTests(unittest.TestCase):
    def _response(self, **kwargs):
        values = {
            "status_code": 200,
            "request_size": 120,
            "upload_size": 20,
            "download_size": 30,
            "header_size": 40,
            "response_size": 70,
            "elapsed": timedelta(seconds=1.25),
            "redirect_count": 0,
        }
        values.update(kwargs)
        return SimpleNamespace(**values)

    def test_native_metrics_count_body_once_and_include_headers(self):
        tracker = ProtocolTrafficTracker()
        tracker.observe(
            method="POST",
            url="https://example.test/api/register?email=secret@example.test",
            response=self._response(),
            elapsed_seconds=99,
        )

        result = tracker.freeze()
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["completed_request_count"], 1)
        self.assertEqual(result["upload_bytes"], 120)
        self.assertEqual(result["request_body_bytes"], 20)
        self.assertEqual(result["download_bytes"], 70)
        self.assertEqual(result["response_body_bytes"], 30)
        self.assertEqual(result["response_header_bytes"], 40)
        self.assertEqual(result["total_bytes"], 190)
        self.assertEqual(result["request_elapsed_seconds"], 1.25)

    def test_http_and_transport_failures_are_each_counted_once(self):
        tracker = ProtocolTrafficTracker()
        tracker.observe(method="GET", url="https://example.test/forbidden", response=self._response(status_code=403))
        tracker.observe(
            method="POST",
            url="https://example.test/api/register",
            response=self._response(status_code=500, request_size=10, upload_size=3, download_size=5, header_size=7),
            error_kind="transport_error",
        )
        result = tracker.freeze()

        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["failed_request_count"], 2)
        self.assertEqual(result["completed_request_count"], 0)
        self.assertEqual(result["http_error_count"], 1)
        self.assertEqual(result["transport_error_count"], 1)
        self.assertEqual(result["upload_bytes"], 130)
        self.assertEqual(result["download_bytes"], 82)

    def test_missing_metrics_are_unknown_and_partial_error_metrics_are_kept(self):
        tracker = ProtocolTrafficTracker()
        tracker.observe(
            method="GET",
            url="https://example.test/partial",
            response=SimpleNamespace(status_code=0, download_size=7, header_size=11),
            error_kind="transport_error",
        )
        tracker.observe(
            method="GET",
            url="https://example.test/missing",
            response=SimpleNamespace(status_code=200),
        )
        result = tracker.freeze()

        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["failed_request_count"], 1)
        self.assertEqual(result["upload_bytes"], 0)
        self.assertEqual(result["response_body_bytes"], 7)
        self.assertEqual(result["response_header_bytes"], 11)
        self.assertEqual(result["download_bytes"], 18)
        self.assertEqual(result["unknown_size_request_count"], 2)
        self.assertEqual(result["unknown_request_body_size_count"], 2)
        self.assertEqual(result["unknown_response_body_size_count"], 1)
        self.assertEqual(result["unknown_response_header_size_count"], 1)
        self.assertEqual(result["unknown_elapsed_request_count"], 2)
        self.assertEqual(result["unknown_status_request_count"], 1)

    def test_partial_native_fields_never_fabricate_complete_totals(self):
        with patch.object(traffic_module._browser_cfg, "BROWSER_TRAFFIC_DETAIL_LOG", True):
            tracker = ProtocolTrafficTracker()
            tracker.observe(
                method="POST",
                url="https://example.test/api/register",
                response=SimpleNamespace(
                    status_code=200,
                    upload_size=23,
                    download_size=7,
                    elapsed=timedelta(seconds=0.25),
                    redirect_count=0,
                ),
            )
            result = tracker.freeze()

        self.assertEqual(result["upload_bytes"], 0)
        self.assertEqual(result["request_body_bytes"], 23)
        self.assertEqual(result["download_bytes"], 0)
        self.assertEqual(result["response_body_bytes"], 7)
        self.assertEqual(result["unknown_size_request_count"], 1)
        self.assertEqual(result["unknown_response_header_size_count"], 1)
        detail = tracker._details[0]
        self.assertIsNone(detail["upload_bytes"])
        self.assertIsNone(detail["download_bytes"])
        self.assertEqual(detail["request_body_bytes"], 23)
        self.assertEqual(detail["response_body_bytes"], 7)
        self.assertEqual(detail["upload_size_source"], "unknown")
        self.assertEqual(detail["download_size_source"], "unknown")
        self.assertFalse(detail["size_complete"])

    def test_redirect_and_stream_are_marked_incomplete_without_consuming_response(self):
        tracker = ProtocolTrafficTracker()
        tracker.observe(
            method="POST",
            url="https://example.test/redirect",
            response=self._response(redirect_count=1),
            streaming=True,
        )
        result = tracker.freeze()

        self.assertEqual(result["redirect_count"], 1)
        self.assertEqual(result["incomplete_size_request_count"], 1)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["total_bytes"], 190)

    def test_detail_is_sanitized_and_bounded_at_collection_time(self):
        with patch.object(traffic_module._browser_cfg, "BROWSER_TRAFFIC_DETAIL_LOG", True), \
             patch.object(traffic_module._browser_cfg, "BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES", 2):
            tracker = ProtocolTrafficTracker()
            for index in range(3):
                tracker.observe(
                    method="GET",
                    url=("https://user:password@example.test/api/secret-token-" + str(index)
                         + "?email=secret@example.test&token=super-secret#fragment"),
                    response=self._response(
                        request_size=index + 1,
                        headers={"Authorization": "Bearer header-canary"},
                        content=b"response-body-canary",
                        request=SimpleNamespace(body=b"request-body-canary"),
                    ),
                )
            self.assertEqual(len(tracker._details), 2)
            result = tracker.freeze()
            target = Mock(spec=logging.Logger)
            tracker.report(target)

        self.assertEqual(result["detail_recorded_count"], 2)
        self.assertEqual(result["detail_omitted_count"], 1)
        self.assertNotIn("details", result)
        serialized = json.dumps(tracker._details, sort_keys=True)
        logged = " ".join(str(call.args) for call in target.info.call_args_list)
        for canary in (
            "password",
            "secret@example.test",
            "super-secret",
            "fragment",
            "secret-token",
            "user",
            "header-canary",
            "response-body-canary",
            "request-body-canary",
        ):
            self.assertNotIn(canary, serialized)
            self.assertNotIn(canary, logged)
        self.assertTrue(all(row["url"].startswith("https://example.test/") for row in tracker._details))

    def test_disabled_detail_and_freeze_isolation(self):
        with patch.object(traffic_module._browser_cfg, "BROWSER_TRAFFIC_DETAIL_LOG", False):
            tracker = ProtocolTrafficTracker()
            tracker.observe(method="GET", url="https://example.test/one", response=self._response())
            frozen = tracker.freeze()
            tracker.observe(method="GET", url="https://example.test/two", response=self._response())
            after = tracker.snapshot()

        self.assertEqual(frozen["request_count"], 1)
        self.assertEqual(after["request_count"], 1)
        self.assertEqual(after["detail_recorded_count"], 0)
        self.assertEqual(tracker._details, [])

    def test_freeze_is_idempotent_with_stable_timestamps(self):
        tracker = ProtocolTrafficTracker()
        first = tracker.freeze()
        second = tracker.freeze()
        self.assertEqual(first, second)
        self.assertIn("started_at", first)
        self.assertIn("finished_at", first)

    def test_report_emits_details_then_summary_and_is_idempotent(self):
        with patch.object(traffic_module._browser_cfg, "BROWSER_TRAFFIC_DETAIL_LOG", True), \
             patch.object(traffic_module._browser_cfg, "BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES", 1):
            tracker = ProtocolTrafficTracker()
            tracker.observe(method="GET", url="https://example.test/one", response=self._response())
            target = Mock(spec=logging.Logger)
            tracker.report(target)
            tracker.report(target)

        self.assertEqual(target.info.call_count, 2)
        first = target.info.call_args_list[0].args[0]
        second = target.info.call_args_list[1].args[0]
        self.assertIn("[协议流量明细]", first)
        self.assertIn("[协议流量汇总]", second)

    def test_non_http_urls_are_excluded(self):
        tracker = ProtocolTrafficTracker()
        tracker.observe(method="GET", url="data:text/plain,secret", response=self._response())
        tracker.observe(method="GET", url="mailto:user@example.test", response=self._response())
        self.assertEqual(tracker.snapshot()["request_count"], 0)

    def test_safe_url_never_keeps_userinfo_query_fragment_or_dynamic_secrets(self):
        values = (
            "https://alice:password@example.test/oauth/secret-token?code=secret-code#otp=123456",
            "https://example.test/email-verify/123456",
            "https://example.test/accounts/123e4567-e89b-12d3-a456-426614174000",
            "https://example.test/callback/AbCdEf0123456789AbCdEf012345",
            "https://SessionKey123.example.test/static/path",
            "https://example.test/reset/A1b2C3d4",
        )
        rendered = [safe_protocol_url(value) for value in values]

        self.assertEqual(rendered[0], "https://example.test/oauth/<redacted>")
        self.assertTrue(all("<redacted>" in value for value in rendered))
        serialized = json.dumps(rendered)
        for canary in (
            "alice",
            "password",
            "secret-code",
            "123456",
            "123e4567-e89b-12d3-a456-426614174000",
            "AbCdEf0123456789AbCdEf012345",
            "sessionkey123",
            "A1b2C3d4",
        ):
            self.assertNotIn(canary, serialized)


class BrowserSessionTrafficObserverTests(unittest.TestCase):
    @staticmethod
    def _bare_session(raw, tracker):
        session = object.__new__(BrowserSession)
        session.traffic_tracker = tracker
        session.session = raw
        session.blocked_until = 0.0
        session.blocked_reason = ""
        session._attach_openai_target_headers_for_url = Mock(side_effect=lambda _url, headers: headers)
        session._observe_response_for_circuit_breaker = Mock(side_effect=lambda response, _url: response)
        return session

    def test_public_get_and_post_wrappers_count_each_raw_send_once(self):
        get_response = SimpleNamespace(
            status_code=200,
            request_size=41,
            upload_size=0,
            download_size=5,
            header_size=11,
            response_size=16,
            elapsed=timedelta(seconds=0.1),
            redirect_count=0,
        )
        post_response = SimpleNamespace(
            status_code=201,
            request_size=67,
            upload_size=9,
            download_size=3,
            header_size=13,
            response_size=16,
            elapsed=timedelta(seconds=0.2),
            redirect_count=0,
        )
        raw = SimpleNamespace(get=Mock(return_value=get_response), post=Mock(return_value=post_response))
        tracker = ProtocolTrafficTracker()
        session = self._bare_session(raw, tracker)
        get_headers = {"accept": "application/json"}
        post_headers = {"content-type": "application/json"}

        self.assertIs(
            session.get("https://example.test/get", headers=get_headers, timeout=3),
            get_response,
        )
        self.assertIs(
            session.post("https://example.test/post", headers=post_headers, json={"value": 1}),
            post_response,
        )

        raw.get.assert_called_once_with(
            "https://example.test/get", headers=get_headers, timeout=3
        )
        raw.post.assert_called_once_with(
            "https://example.test/post", headers=post_headers, json={"value": 1}
        )
        result = tracker.freeze()
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["completed_request_count"], 2)
        self.assertEqual(result["upload_bytes"], 108)
        self.assertEqual(result["request_body_bytes"], 9)
        self.assertEqual(result["download_bytes"], 32)

    def test_public_wrappers_without_tracker_preserve_raw_calls(self):
        get_response = object()
        post_response = object()
        raw = SimpleNamespace(get=Mock(return_value=get_response), post=Mock(return_value=post_response))
        session = self._bare_session(raw, None)

        self.assertIs(session.get("https://example.test/get", headers=None, stream=True), get_response)
        self.assertIs(session.post("https://example.test/post", headers=None, data=b"x"), post_response)
        raw.get.assert_called_once_with("https://example.test/get", headers=None, stream=True)
        raw.post.assert_called_once_with("https://example.test/post", headers=None, data=b"x")

    def test_observer_failure_does_not_change_response_or_raise(self):
        session = object.__new__(BrowserSession)
        session.traffic_tracker = SimpleNamespace(observe=Mock(side_effect=RuntimeError("telemetry")))
        sender = Mock(return_value=SimpleNamespace(status_code=200))
        response = session._send_with_traffic("GET", "https://example.test/path", sender)
        self.assertIs(response, sender.return_value)
        sender.assert_called_once_with("https://example.test/path")

    def test_transport_exception_is_observed_and_original_exception_is_preserved(self):
        observed = Mock()
        session = object.__new__(BrowserSession)
        session.traffic_tracker = SimpleNamespace(observe=observed)
        expected = RuntimeError("network failure")
        sender = Mock(side_effect=expected)
        with self.assertRaisesRegex(RuntimeError, "network failure") as context:
            session._send_with_traffic("POST", "https://example.test/path", sender, stream=True)
        self.assertIs(context.exception, expected)
        self.assertEqual(observed.call_args.kwargs["error_kind"], "transport_error")
        self.assertTrue(observed.call_args.kwargs["streaming"])

    def test_constructor_geo_request_is_observed_before_profile_setup(self):
        response = SimpleNamespace(
            status_code=200,
            request_size=10,
            upload_size=0,
            download_size=4,
            header_size=6,
            response_size=10,
            elapsed=timedelta(seconds=0.1),
            json=lambda: {"country_code": "US", "timezone": "UTC"},
        )
        raw = Mock()
        raw.get.return_value = response
        raw.cookies = SimpleNamespace(set=Mock(), jar=[])
        raw.close = Mock()
        tracker = ProtocolTrafficTracker()
        with patch.object(session_module, "Session", return_value=raw), \
             patch.object(session_module, "_GEO_CACHE", {}), \
             patch("config.browser.AUTO_BROWSER_LOCALE_FROM_IP", True), \
             patch("config.browser.IP_GEO_ENDPOINTS", ["https://geo.example.test/location"]):
            session = BrowserSession(proxy="", traffic_tracker=tracker)

        self.assertEqual(raw.get.call_count, 1)
        self.assertEqual(tracker.snapshot()["request_count"], 1)
        session.close()


if __name__ == "__main__":
    unittest.main()
