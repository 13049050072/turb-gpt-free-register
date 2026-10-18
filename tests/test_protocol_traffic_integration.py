# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from core import registration_service as service


class ProtocolTrafficRegistrationIntegrationTests(unittest.TestCase):
    def test_failed_protocol_registration_returns_bounded_traffic_summary(self):
        session = SimpleNamespace(
            proxy=None,
            device_id="device-test",
            auth_session_logging_id="auth-log-test",
            browser_profile={},
            sentinel_sid="sentinel-test",
        )
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"), \
             patch.object(main, "BrowserSession", return_value=session), \
             patch.object(
                 main,
                 "_run_pre_otp_authorization_with_direct_fallback",
                 side_effect=RuntimeError("HTTP 403 with token=must-not-enter-traffic"),
             ), \
             patch("core.email_provider.release_email_if_unconsumed", return_value=True), \
             patch("core.email_provider.resolve_email_source", return_value="generic_api"):
            result = main.run_registration(
                email="user@example.com",
                name="Test User",
                birthday="2000-01-01",
            )

        self.assertFalse(result["success"])
        traffic = result["network_traffic"]
        self.assertTrue(traffic["available"])
        self.assertEqual(traffic["request_count"], 0)
        self.assertNotIn("must-not-enter-traffic", str(traffic))

    def test_constructor_failure_uses_unified_failure_result_with_summary(self):
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "protocol"), patch.object(
            main,
            "BrowserSession",
            side_effect=RuntimeError("constructor failed"),
        ), patch("core.email_provider.release_email_if_unconsumed", return_value=True), patch(
            "core.email_provider.resolve_email_source",
            return_value="generic_api",
        ):
            result = main.run_registration(
                email="user@example.com",
                name="Test User",
                birthday="2000-01-01",
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "constructor failed")
        self.assertTrue(result["network_traffic"]["available"])
        self.assertEqual(result["network_traffic"]["request_count"], 0)

    def test_broken_freeze_and_report_return_unavailable_summary(self):
        tracker = SimpleNamespace(
            freeze=Mock(return_value=None),
            report=Mock(side_effect=RuntimeError("logger failed")),
        )

        result = main._freeze_protocol_traffic(tracker)

        self.assertFalse(result["available"])
        self.assertEqual(result["method"], "curl_cffi.protocol")
        tracker.freeze.assert_called_once_with()
        tracker.report.assert_called_once_with(main.logger)

    def test_registration_job_persists_success_and_failure_traffic(self):
        traffic = {
            "available": True,
            "method": "curl_cffi.protocol",
            "label": "Protocol",
            "request_count": 2,
            "upload_bytes": 10,
            "download_bytes": 20,
            "total_bytes": 30,
        }
        cases = (
            ({"success": True, "email": "user@example.com", "account_id": 7, "network_traffic": traffic}, "success"),
            ({"success": False, "email": "user@example.com", "error": "failed", "network_traffic": traffic}, "failed"),
        )
        for index, (registration_result, expected_status) in enumerate(cases, start=1):
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as tmp:
                log_file = str(Path(tmp) / "job.log")
                job = {
                    "id": index,
                    "status": "pending",
                    "email": "user@example.com",
                    "log_file": log_file,
                }
                update_job = Mock()
                with patch.object(service, "_activate_job"), patch.object(
                    service,
                    "_deactivate_job",
                ), patch.object(service.db, "get_job", return_value=job), patch.object(
                    service.db,
                    "update_job",
                    update_job,
                ), patch.object(
                    service,
                    "_prepare_registration_args",
                    return_value=("user@example.com", "Test User", "2000-01-01"),
                ), patch.object(service, "check_stop_requested"), patch.object(
                    service,
                    "is_stop_requested",
                    return_value=False,
                ), patch.object(service, "_release_unconsumed_job_email"), patch(
                    "main.run_registration",
                    return_value=registration_result,
                ):
                    service._run_one_job(index, log_file)

            terminal = [
                call
                for call in update_job.call_args_list
                if call.kwargs.get("status") == expected_status
            ]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0].kwargs["network_traffic"], traffic)

    def test_non_protocol_driver_does_not_construct_protocol_tracker_or_session(self):
        with patch.object(main._roxy_cfg, "REGISTRATION_DRIVER", "roxy"), \
             patch.object(main, "ProtocolTrafficTracker") as tracker, \
             patch("core.roxy_registration.run_roxy_registration", return_value={"success": True}) as run_roxy:
            result = main.run_registration(
                email="user@example.com",
                name="Test User",
                birthday="2000-01-01",
            )

        self.assertEqual(result, {"success": True})
        tracker.assert_not_called()
        run_roxy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
