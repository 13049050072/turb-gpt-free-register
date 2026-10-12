import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from core.sub2api_401_service import Sub2Api401Service, Sub2ApiSnapshotConflict


class Sub2Api401ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.audit_path = Path(self.temp_dir.name) / "sub2api-401-audit.json"

    def service(self, *, rows, refresh=None, delete=None, audit_path=None):
        def default_refresh(account_ids):
            return {"total": len(account_ids), "success": len(account_ids), "failed": 0, "errors": []}

        return Sub2Api401Service(
            list_fn=lambda: list(rows),
            refresh_fn=refresh or default_refresh,
            delete_fn=delete or (lambda account_id: {"ok": True, "account_id": account_id}),
            audit_path=audit_path or self.audit_path,
        )

    def test_detect_classifies_healthy_unauthorized_timeout_and_unknown(self):
        rows = [
            {"account_id": "healthy", "email": "healthy@example.com"},
            {"account_id": "bad", "email": "bad@example.com"},
            {"account_id": "slow", "email": "slow@example.com"},
            {"account_id": "odd", "email": "odd@example.com"},
        ]

        def refresh(account_ids):
            return {
                "total": 4,
                "success": 1,
                "failed": 3,
                "errors": [
                    {"account_id": "bad", "error": "HTTP 401 unauthorized"},
                    {"account_id": "slow", "error": "request timeout"},
                    {"account_id": "odd", "error": "provider unavailable"},
                ],
            }

        result = self.service(rows=rows, refresh=refresh).detect()
        by_id = {item["account_id"]: item for item in result["items"]}
        self.assertEqual("pending", by_id["bad"]["state"])
        self.assertTrue(by_id["bad"]["retryable"])
        self.assertEqual("timeout", by_id["slow"]["health"])
        self.assertEqual("unknown", by_id["odd"]["health"])
        self.assertNotIn("healthy", by_id)
        self.assertEqual({"healthy": 1, "unauthorized": 1, "timeout": 1, "unknown": 1}, {
            key: result["counts"][key] for key in ("healthy", "unauthorized", "timeout", "unknown")
        })

    def test_pre_retry_snapshot_persists_across_restart(self):
        rows = [{"account_id": "old", "email": "one@example.com", "account_name": "one"}]
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": 1, "success": 0, "failed": 1, "errors": [{"account_id": "old", "error": "401"}]},
        )
        service.detect()
        service.mark_retrying(["old"], batch_id="batch-1")

        payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
        self.assertEqual(["old"], payload["items"]["old"]["pre_retry_account_ids"])
        reloaded = self.service(rows=rows).state()["items"][0]
        self.assertEqual("retrying", reloaded["state"])
        self.assertEqual("batch-1", reloaded["retry_batch_id"])
        self.assertEqual(["old"], reloaded["pre_retry_account_ids"])

    def test_unique_new_same_email_account_becomes_cleanup_ready(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        errors = {"old": "401 unauthorized"}

        def refresh(ids):
            failed = [{"account_id": account_id, "error": errors[account_id]} for account_id in ids if account_id in errors]
            return {"total": len(ids), "success": len(ids) - len(failed), "failed": len(failed), "errors": failed}

        service = self.service(rows=rows, refresh=refresh)
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("cleanup_ready", item["state"])
        self.assertEqual("new", item["replacement_account_id"])
        self.assertEqual("old", item["cleanup_account_id"])
        self.assertEqual(["old"], item["pre_retry_account_ids"])
        self.assertEqual(["new", "old"], item["post_retry_account_ids"])

    def test_detect_reconciles_cleanup_ready_when_locked_old_id_disappears(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]

        def refresh(ids):
            failures = [
                {"account_id": value, "error": "401 unauthorized"}
                for value in ids
                if value == "old"
            ]
            return {
                "total": len(ids),
                "success": len(ids) - len(failures),
                "failed": len(failures),
                "errors": failures,
            }

        service = self.service(rows=rows, refresh=refresh)
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})
        rows[:] = [{"account_id": "new", "email": "one@example.com"}]

        reloaded = self.service(rows=rows, refresh=refresh)
        item = reloaded.detect()["items"][0]

        self.assertEqual("cleaned", item["state"])
        self.assertEqual("new", item["replacement_account_id"])
        self.assertEqual("old", item["cleanup_account_id"])

    def test_detect_blocks_cleanup_ready_when_verified_snapshot_changes(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]

        def refresh(ids):
            failures = [
                {"account_id": value, "error": "401 unauthorized"}
                for value in ids
                if value == "old"
            ]
            return {
                "total": len(ids),
                "success": len(ids) - len(failures),
                "failed": len(failures),
                "errors": failures,
            }

        service = self.service(rows=rows, refresh=refresh)
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})
        rows.append({"account_id": "unexpected", "email": "one@example.com"})

        item = service.detect()["items"][0]

        self.assertEqual("ambiguous", item["state"])
        self.assertEqual("", item["cleanup_account_id"])
        self.assertIn("快照发生变化", item["reason"])

    def test_historical_all_healthy_duplicates_keep_unique_latest_out_of_items(self):
        rows = [
            {"account_id": "old-a", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "old-b", "email": "one@example.com", "created_at": "2026-10-12T00:00:00Z"},
            {"account_id": "keeper", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]

        result = self.service(rows=rows).detect()
        by_id = {item["account_id"]: item for item in result["items"]}

        self.assertEqual({"old-a", "old-b"}, set(by_id))
        for account_id in ("old-a", "old-b"):
            with self.subTest(account_id=account_id):
                self.assertEqual("cleanup_ready", by_id[account_id]["state"])
                self.assertEqual("keeper", by_id[account_id]["replacement_account_id"])
                self.assertEqual(account_id, by_id[account_id]["cleanup_account_id"])
                self.assertIn("唯一最新账号为 keeper", by_id[account_id]["reason"])

    def test_historical_healthy_duplicates_without_created_at_remain_ambiguous(self):
        rows = [
            {"account_id": "a", "email": "one@example.com"},
            {"account_id": "b", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]

        result = self.service(rows=rows).detect()

        self.assertEqual({"a", "b"}, {item["account_id"] for item in result["items"]})
        self.assertTrue(all(item["state"] == "ambiguous" for item in result["items"]))
        self.assertTrue(all("缺少合法 created_at" in item["reason"] for item in result["items"]))

    def test_historical_healthy_duplicates_with_tied_latest_remain_ambiguous(self):
        rows = [
            {"account_id": "a", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
            {"account_id": "b", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
            {"account_id": "older", "email": "one@example.com", "created_at": "2026-10-12T00:00:00Z"},
        ]

        result = self.service(rows=rows).detect()

        self.assertEqual(3, len(result["items"]))
        self.assertTrue(all(item["state"] == "ambiguous" for item in result["items"]))
        self.assertTrue(all("created_at 并列最新" in item["reason"] for item in result["items"]))

    def test_historical_unique_healthy_keeper_marks_all_unauthorized_older_ids(self):
        rows = [
            {"account_id": "old-a", "email": "one@example.com"},
            {"account_id": "old-b", "email": "one@example.com"},
            {"account_id": "keeper", "email": "one@example.com"},
        ]

        def refresh(ids):
            errors = [
                {"account_id": account_id, "error": "401 unauthorized"}
                for account_id in ids
                if account_id != "keeper"
            ]
            return {
                "total": len(ids),
                "success": len(ids) - len(errors),
                "failed": len(errors),
                "errors": errors,
            }

        result = self.service(rows=rows, refresh=refresh).detect()
        by_id = {item["account_id"]: item for item in result["items"]}

        self.assertEqual({"old-a", "old-b"}, set(by_id))
        self.assertTrue(all(item["state"] == "cleanup_ready" for item in by_id.values()))
        self.assertTrue(all(item["replacement_account_id"] == "keeper" for item in by_id.values()))
        self.assertTrue(all("唯一健康账号为 keeper" in item["reason"] for item in by_id.values()))

    def test_historical_cleanup_deletes_only_exact_old_ids(self):
        rows = [
            {"account_id": "old-a", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "old-b", "email": "one@example.com", "created_at": "2026-10-12T00:00:00Z"},
            {"account_id": "keeper", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]
        deleted = []

        def delete(account_id):
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            return {"ok": True}

        service = self.service(rows=rows, delete=delete)
        service.detect()
        result = service.cleanup(["keeper", "old-b", "old-a", "old-b", "missing"], opt_in=True)

        self.assertCountEqual(["old-a", "old-b"], deleted)
        self.assertEqual(["keeper"], [row["account_id"] for row in rows])
        self.assertEqual({"old-a", "old-b"}, {item["account_id"] for item in result["items"]})
        self.assertTrue(all(item["ok"] for item in result["items"]))
        self.assertTrue(all(item["state"] == "cleaned" for item in service.state()["items"]))

    def test_historical_cleanup_allows_sequential_exact_id_cleanup_after_restart(self):
        rows = [
            {"account_id": "old-a", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "old-b", "email": "one@example.com", "created_at": "2026-10-12T00:00:00Z"},
            {"account_id": "keeper", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]
        deleted = []

        def delete(account_id):
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            return {"ok": True}

        service = self.service(rows=rows, delete=delete)
        service.detect()
        first = service.cleanup(["old-a"], opt_in=True)
        self.assertTrue(first["items"][0]["ok"])

        reloaded = self.service(rows=rows, delete=delete)
        second = reloaded.cleanup(["old-b"], opt_in=True)

        self.assertTrue(second["items"][0]["ok"])
        self.assertEqual(["old-a", "old-b"], deleted)
        self.assertEqual(["keeper"], [row["account_id"] for row in rows])

    def test_cleanup_blocks_when_same_email_snapshot_changes(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        errors = {"old": "401 unauthorized"}
        deleted = []

        def refresh(ids):
            failures = [
                {"account_id": value, "error": errors[value]}
                for value in ids
                if value in errors
            ]
            return {
                "total": len(ids),
                "success": len(ids) - len(failures),
                "failed": len(failures),
                "errors": failures,
            }

        service = self.service(
            rows=rows,
            refresh=refresh,
            delete=lambda account_id: deleted.append(account_id) or {"ok": True},
        )
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})
        rows.append({"account_id": "unexpected", "email": "one@example.com"})

        result = service.cleanup(["old"], opt_in=True)

        self.assertEqual([], deleted)
        self.assertFalse(result["items"][0]["ok"])
        item = service.state()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertIn("快照已变化", item["reason"])

    def test_no_new_account_is_ambiguous_and_not_cleanup_ready(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": 1, "success": 0, "failed": 1, "errors": [{"account_id": "old", "error": "401"}]},
        )
        service.detect()
        service.mark_retrying(["old"])
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertEqual("", item["cleanup_account_id"])

    def test_multiple_new_accounts_are_ambiguous(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        errors = {"old": "401"}

        def refresh(ids):
            failures = [{"account_id": value, "error": errors[value]} for value in ids if value in errors]
            return {"total": len(ids), "success": len(ids) - len(failures), "failed": len(failures), "errors": failures}

        service = self.service(rows=rows, refresh=refresh)
        service.detect()
        service.mark_retrying(["old"])
        rows.extend([
            {"account_id": "new-a", "email": "one@example.com"},
            {"account_id": "new-b", "email": "one@example.com"},
        ])
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertEqual("", item["replacement_account_id"])

    def test_old_account_missing_blocks_cleanup(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": len(ids), "success": 0, "failed": len(ids), "errors": [{"account_id": value, "error": "401"} for value in ids]},
        )
        service.detect()
        service.mark_retrying(["old"])
        rows[:] = [{"account_id": "new", "email": "one@example.com"}]
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertEqual("", item["cleanup_account_id"])

    def test_local_retry_failure_preserves_old_id_and_blocks_cleanup(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": 1, "success": 0, "failed": 1, "errors": [{"account_id": "old", "error": "invalid_grant"}]},
        )
        service.detect()
        service.mark_retrying(["old"])
        item = service.detect({"one@example.com": "failed"})["items"][0]
        self.assertEqual("failed", item["state"])
        self.assertTrue(item["retryable"])
        self.assertEqual(["old"], item["pre_retry_account_ids"])
        self.assertEqual("", item["cleanup_account_id"])

    def test_email_missing_or_conflicting_is_ambiguous(self):
        rows = [
            {"account_id": "missing", "email": "", "email_ambiguous": True},
            {"account_id": "dup-a", "email": "dup@example.com"},
            {"account_id": "dup-b", "email": "dup@example.com"},
        ]
        result = self.service(
            rows=rows,
            refresh=lambda ids: {"total": len(ids), "success": 0, "failed": len(ids), "errors": [{"account_id": value, "error": "401"} for value in ids]},
        ).detect()
        self.assertTrue(result["items"])
        self.assertTrue(all(item["state"] == "ambiguous" for item in result["items"]))
        self.assertTrue(all(not item["retryable"] for item in result["items"]))

    def test_snapshot_change_aborts_before_oauth(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": 1, "success": 0, "failed": 1, "errors": [{"account_id": "old", "error": "401"}]},
        )
        service.detect()
        rows.append({"account_id": "other", "email": "one@example.com"})
        with self.assertRaises(Sub2ApiSnapshotConflict) as captured:
            service.mark_retrying(["old"])
        self.assertEqual(["old"], captured.exception.account_ids)
        self.assertEqual("ambiguous", service.state()["items"][0]["state"])

    def test_cleanup_requires_opt_in_and_deletes_only_locked_old_id(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        errors = {"old": "401"}
        deleted = []

        def refresh(ids):
            failures = [{"account_id": value, "error": errors[value]} for value in ids if value in errors]
            return {"total": len(ids), "success": len(ids) - len(failures), "failed": len(failures), "errors": failures}

        def delete(account_id):
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            return {"ok": True}

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})

        preview = service.cleanup(["old"], opt_in=False)
        self.assertFalse(preview["cleanup_performed"])
        self.assertEqual([], deleted)
        result = service.cleanup(["old"], opt_in=True)
        self.assertEqual(["old"], deleted)
        self.assertTrue(result["items"][0]["ok"])
        self.assertEqual("deleted_and_verified", result["items"][0]["result"])
        self.assertEqual("cleaned", service.state()["items"][0]["state"])
        self.assertEqual("old", service.state()["items"][0]["cleanup_account_id"])

    def test_cleanup_waits_for_detection_state_commit_and_does_not_delete_stale_target(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        errors = {"old": "401"}
        deleted = []
        block_refresh = False
        refresh_started = threading.Event()
        release_refresh = threading.Event()

        def refresh(ids):
            if block_refresh:
                refresh_started.set()
                self.assertTrue(release_refresh.wait(2))
            failures = [{"account_id": value, "error": errors[value]} for value in ids if value in errors]
            return {"total": len(ids), "success": len(ids) - len(failures), "failed": len(failures), "errors": failures}

        def delete(account_id):
            deleted.append(account_id)
            return {"ok": True}

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})
        self.assertEqual("cleanup_ready", service.state()["items"][0]["state"])

        rows[:] = [{"account_id": "old", "email": "one@example.com"}]
        block_refresh = True
        detect_thread = threading.Thread(target=lambda: service.detect({"one@example.com": "success"}))
        cleanup_result = []
        cleanup_thread = threading.Thread(target=lambda: cleanup_result.append(service.cleanup(["old"], opt_in=True)))
        detect_thread.start()
        self.assertTrue(refresh_started.wait(2))
        cleanup_thread.start()
        self.assertTrue(cleanup_thread.is_alive())
        release_refresh.set()
        detect_thread.join(2)
        cleanup_thread.join(2)

        self.assertFalse(detect_thread.is_alive())
        self.assertFalse(cleanup_thread.is_alive())
        self.assertEqual([], deleted)
        self.assertFalse(cleanup_result[0]["cleanup_performed"])
        self.assertEqual("ambiguous", service.state()["items"][0]["state"])

    def test_cleanup_failure_preserves_retry_evidence(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        errors = {"old": "401"}

        def refresh(ids):
            failures = [{"account_id": value, "error": errors[value]} for value in ids if value in errors]
            return {"total": len(ids), "success": len(ids) - len(failures), "failed": len(failures), "errors": failures}

        service = self.service(rows=rows, refresh=refresh, delete=lambda account_id: {"ok": False})
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})
        result = service.cleanup(["old"], opt_in=True)
        self.assertFalse(result["items"][0]["ok"])
        item = service.state()["items"][0]
        self.assertEqual("cleanup_ready", item["state"])
        self.assertEqual("old", item["cleanup_account_id"])
        self.assertEqual("new", item["replacement_account_id"])

    def test_cleanup_requires_healthy_replacement_after_delete(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        new_only_refreshes = 0
        deleted = []

        def refresh(ids):
            nonlocal new_only_refreshes
            if ids == ["new"]:
                new_only_refreshes += 1
                if new_only_refreshes >= 2:
                    return {
                        "total": 1,
                        "success": 0,
                        "failed": 1,
                        "errors": [{"account_id": "new", "error": "401 unauthorized"}],
                    }
            failures = [
                {"account_id": value, "error": "401 unauthorized"}
                for value in ids
                if value == "old"
            ]
            return {
                "total": len(ids),
                "success": len(ids) - len(failures),
                "failed": len(failures),
                "errors": failures,
            }

        def delete(account_id):
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            return {"ok": True}

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})

        result = service.cleanup(["old"], opt_in=True)

        self.assertEqual(["old"], deleted)
        self.assertFalse(result["items"][0]["ok"])
        item = service.state()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertIn("清理后替代 account_id 未通过刷新验证", item["reason"])

    def test_cleanup_reconciles_missing_old_id_after_relist_failure_and_restart(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        deleted = []
        fail_next_list = False

        def list_accounts():
            nonlocal fail_next_list
            if fail_next_list:
                fail_next_list = False
                raise RuntimeError("transient list failure with secret detail")
            return list(rows)

        def refresh(ids):
            failures = [
                {"account_id": value, "error": "401 unauthorized"}
                for value in ids
                if value == "old"
            ]
            return {
                "total": len(ids),
                "success": len(ids) - len(failures),
                "failed": len(failures),
                "errors": failures,
            }

        def delete(account_id):
            nonlocal fail_next_list
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            fail_next_list = True
            return {"ok": True}

        service = Sub2Api401Service(
            list_fn=list_accounts,
            refresh_fn=refresh,
            delete_fn=delete,
            audit_path=self.audit_path,
        )
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})

        first = service.cleanup(["old"], opt_in=True)
        self.assertFalse(first["items"][0]["ok"])
        self.assertEqual("cleanup_ready", service.state()["items"][0]["state"])
        self.assertNotIn("secret detail", self.audit_path.read_text(encoding="utf-8"))

        reloaded = Sub2Api401Service(
            list_fn=lambda: list(rows),
            refresh_fn=refresh,
            delete_fn=delete,
            audit_path=self.audit_path,
        )
        second = reloaded.cleanup(["old"], opt_in=True)

        self.assertTrue(second["items"][0]["ok"])
        self.assertEqual("absent_and_verified", second["items"][0]["result"])
        self.assertEqual(["old"], deleted)
        self.assertEqual("cleaned", reloaded.state()["items"][0]["state"])

    def test_audit_write_is_atomic_private_and_symlink_target_safe(self):
        target_dir = Path(self.temp_dir.name) / "data"
        target_dir.mkdir()
        target = target_dir / "sub2api_401_audit.json"
        link = Path(self.temp_dir.name) / "sub2api_401_audit.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unsupported")
        rows = [{"account_id": "old", "email": "one@example.com"}]
        self.service(
            rows=rows,
            refresh=lambda ids: {"total": 1, "success": 0, "failed": 1, "errors": [{"account_id": "old", "error": "401 'access_token': 'secret' Bearer eyJsecret.token"}]},
            audit_path=link,
        ).detect()
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())
        persisted = target.read_text(encoding="utf-8")
        self.assertNotIn("'secret'", persisted)
        self.assertNotIn("eyJsecret.token", persisted)
        if os.name != "nt":
            self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertEqual([], list(target_dir.glob("*.tmp")))

    def test_selection_uses_account_ids_and_rejects_untracked(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": 1, "success": 0, "failed": 1, "errors": [{"account_id": "old", "error": "401"}]},
        )
        service.detect()
        selected, skipped = service.select_retryable(["old", "old", "missing", ""])
        self.assertEqual(["old"], [item["account_id"] for item in selected])
        self.assertEqual(2, len(skipped))


if __name__ == "__main__":
    unittest.main()
