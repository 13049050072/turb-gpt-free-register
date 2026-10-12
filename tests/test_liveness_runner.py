# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import live_check_service, liveness_runner


class LivenessRunnerTests(unittest.TestCase):
    def test_run_liveness_check_reuses_normal_worker_entry_point(self):
        result = {"ok": True, "status": "live", "access_token": "new-secret"}
        with patch(
            "core.live_check_service.run_account_live_check",
            return_value=result,
        ) as run:
            actual = liveness_runner.run_liveness_check(7)
        self.assertIs(result, actual)
        run.assert_called_once_with(7, trigger="chatgpt2api-401", proxy=None)

    def test_sync_worker_claims_account_and_uses_normal_execution_chain(self):
        result = {"ok": True, "status": "live", "access_token": "new-secret"}
        account = {"id": 7, "email": "one@example.com", "email_source": "remail"}
        with patch.object(live_check_service.db, "get_account", return_value=account), patch.object(
            live_check_service.db, "claim_account_live_check", return_value=True
        ) as claim, patch.object(live_check_service, "_append_log") as append, patch.object(
            live_check_service, "_run_live_check", return_value=result
        ) as run:
            actual = live_check_service.run_account_live_check(7)
        self.assertIs(result, actual)
        claim.assert_called_once_with(acc_id=7, trigger="chatgpt2api-401")
        append.assert_called_once()
        run.assert_called_once_with(
            account_id=7,
            email="one@example.com",
            proxy=None,
            trigger="chatgpt2api-401",
            release_queue_slot=False,
        )

    def test_sync_worker_rejects_concurrent_normal_liveness(self):
        account = {"id": 7, "email": "one@example.com"}
        with patch.object(live_check_service.db, "get_account", return_value=account), patch.object(
            live_check_service.db, "claim_account_live_check", return_value=False
        ), patch.object(live_check_service, "_run_live_check") as run:
            actual = live_check_service.run_account_live_check(7)
        self.assertFalse(actual["ok"])
        self.assertEqual("busy", actual["status"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
