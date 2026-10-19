# -*- coding: utf-8 -*-
"""全量套餐查询后台队列的单元测试。"""
import threading
import time
import unittest
from unittest.mock import patch

from core import plan_check_service


def _wait_idle(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not plan_check_service.bulk_check_status().get("running"):
            return True
        time.sleep(0.02)
    return False


class BulkPlanCheckTests(unittest.TestCase):
    def tearDown(self):
        _wait_idle()

    def _run_with(self, rows, queue_results):
        """rows: 账号行；queue_results: 每次 enqueue 依次返回的结果。"""
        calls = []
        results = list(queue_results)

        def fake_enqueue(**kwargs):
            calls.append(kwargs)
            if results:
                return results.pop(0)
            return {"accepted": True}

        with patch.object(plan_check_service.db, "list_accounts", return_value=rows), \
             patch.object(plan_check_service.db, "get_account",
                          side_effect=lambda acc_id: next(
                              (row for row in rows if int(row["id"]) == int(acc_id)), None)), \
             patch.object(plan_check_service, "enqueue_account_plan_check", side_effect=fake_enqueue):
            started = plan_check_service.start_bulk_all_check()
            self.assertTrue(started["started"])
            self.assertTrue(_wait_idle())
        return calls, plan_check_service.bulk_check_status()

    def test_all_accounts_are_enqueued(self):
        rows = [
            {"id": 1, "email": "a@example.test", "access_token": "t1"},
            {"id": 2, "email": "b@example.test", "access_token": "t2"},
            {"id": 3, "email": "c@example.test", "access_token": ""},
        ]
        calls, state = self._run_with(rows, [])

        self.assertEqual([call["account_id"] for call in calls], [1, 2])
        self.assertEqual(state["total"], 2)
        self.assertEqual(state["queued"], 2)
        self.assertEqual(state["failed"], 0)
        self.assertEqual(state["processed"], 2)
        self.assertFalse(state["running"])
        self.assertTrue(state["finished_at"])

    def test_queue_full_is_retried_until_accepted(self):
        rows = [{"id": 7, "email": "q@example.test", "access_token": "t"}]
        calls, state = self._run_with(rows, [
            {"accepted": False, "queue_full": True},
            {"accepted": False, "queue_full": True},
            {"accepted": True},
        ])

        self.assertEqual(len(calls), 3)
        self.assertEqual(state["queued"], 1)
        self.assertEqual(state["failed"], 0)

    def test_busy_account_is_counted_as_skipped(self):
        rows = [{"id": 9, "email": "busy@example.test", "access_token": "t"}]
        calls, state = self._run_with(rows, [{"accepted": False, "busy": True}])

        self.assertEqual(len(calls), 1)
        self.assertEqual(state["skipped"], 1)
        self.assertEqual(state["queued"], 0)

    def test_enqueue_exception_does_not_abort_run(self):
        rows = [
            {"id": 21, "email": "x@example.test", "access_token": "t1"},
            {"id": 22, "email": "y@example.test", "access_token": "t2"},
        ]
        calls = []

        def flaky(**kwargs):
            calls.append(kwargs["account_id"])
            if kwargs["account_id"] == 21:
                raise RuntimeError("boom")
            return {"accepted": True}

        with patch.object(plan_check_service.db, "list_accounts", return_value=rows), \
             patch.object(plan_check_service.db, "get_account",
                          side_effect=lambda acc_id: next(
                              (row for row in rows if int(row["id"]) == int(acc_id)), None)), \
             patch.object(plan_check_service, "enqueue_account_plan_check", side_effect=flaky):
            plan_check_service.start_bulk_all_check()
            self.assertTrue(_wait_idle())

        state = plan_check_service.bulk_check_status()
        self.assertEqual(calls, [21, 22])
        self.assertEqual(state["failed"], 1)
        self.assertEqual(state["queued"], 1)
        self.assertEqual(state["processed"], 2)

    def test_second_start_reports_already_running(self):
        rows = [{"id": 11, "email": "slow@example.test", "access_token": "t"}]
        release = threading.Event()

        def slow_enqueue(**_kwargs):
            release.wait(3.0)
            return {"accepted": True}

        with patch.object(plan_check_service.db, "list_accounts", return_value=rows), \
             patch.object(plan_check_service.db, "get_account", return_value=rows[0]), \
             patch.object(plan_check_service, "enqueue_account_plan_check", side_effect=slow_enqueue):
            first = plan_check_service.start_bulk_all_check()
            self.assertTrue(first["started"])
            second = plan_check_service.start_bulk_all_check()
            self.assertFalse(second["started"])
            self.assertTrue(second["already_running"])
            release.set()
            self.assertTrue(_wait_idle())


if __name__ == "__main__":
    unittest.main()
