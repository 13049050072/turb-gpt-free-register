# -*- coding: utf-8 -*-
"""注册任务重启恢复的单元测试。"""
import unittest
from unittest.mock import patch

from core import registration_service


class _FakeExecutor:
    def __init__(self, fail=False):
        self.fail = fail
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        if self.fail:
            raise RuntimeError("pool closed")
        self.submitted.append((fn, args))
        return None


class RegistrationRecoveryTests(unittest.TestCase):
    def _jobs(self):
        return [
            {"id": 1, "status": "running", "log_file": "/tmp/1.log"},
            {"id": 2, "status": "stopping", "log_file": "/tmp/2.log"},
            {"id": 3, "status": "pending", "log_file": "/tmp/3.log"},
            {"id": 4, "status": "success", "log_file": "/tmp/4.log"},
            {"id": 5, "status": "failed", "log_file": "/tmp/5.log"},
            {"id": 6, "status": "cancelled", "log_file": "/tmp/6.log"},
        ]

    def test_running_jobs_fail_and_pending_resume(self):
        updates = []
        executor = _FakeExecutor()
        with patch.object(registration_service.db, "list_jobs", return_value=self._jobs()), \
             patch.object(registration_service.db, "update_job",
                          side_effect=lambda job_id, **kwargs: updates.append((job_id, kwargs))), \
             patch.object(registration_service, "get_executor", return_value=executor):
            result = registration_service.recover_interrupted_jobs()

        self.assertEqual(result, {"interrupted": 2, "resumed": 1})
        failed = {job_id: kw for job_id, kw in updates}
        self.assertEqual(set(failed), {1, 2})
        for job_id in (1, 2):
            self.assertEqual(failed[job_id]["status"], "failed")
            self.assertIn("重启", failed[job_id]["error"])
            self.assertTrue(failed[job_id]["completed_at"])
        self.assertEqual(len(executor.submitted), 1)
        self.assertEqual(executor.submitted[0][1][0], 3)

    def test_submit_failure_marks_job_failed(self):
        updates = []
        executor = _FakeExecutor(fail=True)
        with patch.object(registration_service.db, "list_jobs", return_value=self._jobs()), \
             patch.object(registration_service.db, "update_job",
                          side_effect=lambda job_id, **kwargs: updates.append((job_id, kwargs))), \
             patch.object(registration_service, "get_executor", return_value=executor):
            result = registration_service.recover_interrupted_jobs()

        self.assertEqual(result, {"interrupted": 2, "resumed": 0})
        pending_update = [kw for job_id, kw in updates if job_id == 3]
        self.assertEqual(len(pending_update), 1)
        self.assertEqual(pending_update[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
