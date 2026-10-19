# -*- coding: utf-8 -*-
"""全量套餐查询接口的接线测试。"""
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from core import account_inspection_service
from webui.app import create_app

HEADERS = {"X-Auth-Code": "test-auth"}


class BulkPlanCheckApiTests(unittest.TestCase):
    def setUp(self):
        with patch.object(
            account_inspection_service,
            "startup_recovery_guard",
            return_value=nullcontext(False),
        ), patch("core.chatgpt2api_import_service.start_import_worker"):
            self.client = create_app(auth_code="test-auth").test_client()

    def test_start_route_returns_progress_snapshot(self):
        snapshot = {
            "started": True, "already_running": False, "running": True,
            "total": 12, "processed": 0, "fed": 0, "queued": 0, "skipped": 0,
            "failed": 0, "started_at": "2026-10-20T00:00:00",
            "finished_at": None, "trigger": "manual_all", "workers": 5,
        }
        with patch("core.plan_check_service.start_bulk_all_check", return_value=snapshot) as started:
            r = self.client.post("/api/accounts/check-plan-all", json={"workers": 5}, headers=HEADERS)
        self.assertEqual(r.status_code, 202)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["total"], 12)
        self.assertEqual(body["workers"], 5)
        started.assert_called_once_with(archived=False, workers=5)

    def test_start_route_rejects_invalid_workers(self):
        with patch("core.plan_check_service.start_bulk_all_check",
                   side_effect=ValueError("并发线程数必须为 1–16 的整数")):
            r = self.client.post("/api/accounts/check-plan-all", json={"workers": 99}, headers=HEADERS)
        self.assertEqual(r.status_code, 400)
        self.assertIn("并发", r.get_json()["error"])

    def test_status_route_reads_service_state(self):
        with patch("core.plan_check_service.bulk_check_status", return_value={
            "running": False, "total": 5, "processed": 5, "fed": 4, "queued": 4,
            "skipped": 1, "failed": 0, "started_at": "x", "finished_at": "y",
            "trigger": "manual_all", "workers": 3,
        }):
            r = self.client.get("/api/accounts/check-plan-all-status", headers=HEADERS)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["processed"], 5)
        self.assertFalse(body["running"])


if __name__ == "__main__":
    unittest.main()
