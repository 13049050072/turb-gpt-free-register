import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from core.sub2api_admin import Sub2ApiAdminClient
from core.sub2api_401_service import Sub2Api401Service, Sub2ApiSnapshotConflict, local_retry_status


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

    def test_detect_refreshes_only_error_status_accounts(self):
        rows = [
            {"account_id": "40", "email": "a@example.com", "status": "active"},
            {"account_id": "41", "email": "b@example.com", "status": "active"},
            {"account_id": "42", "email": "c@example.com", "status": "error"},
        ]
        refreshed = []

        def refresh(ids):
            refreshed.append(list(ids))
            return {"total": 1, "success": 0, "failed": 1, "errors": [{"account_id": "42", "error": "Token revoked (401)"}]}

        result = self.service(rows=rows, refresh=refresh).detect()
        self.assertEqual([["42"]], refreshed)
        self.assertEqual(["42"], [item["account_id"] for item in result["items"]])
        self.assertEqual("pending", result["items"][0]["state"])
        self.assertEqual(1, result["counts"]["probed"])
        self.assertEqual(2, result["counts"]["healthy"])
        self.assertEqual(1, result["counts"]["unauthorized"])

    def test_detect_without_error_accounts_sends_no_refresh(self):
        refreshed = []
        rows = [{"account_id": "40", "email": "a@example.com", "status": "active"}]
        result = self.service(rows=rows, refresh=lambda ids: refreshed.append(ids) or {}).detect()
        self.assertEqual([], refreshed)
        self.assertEqual([], result["items"])
        self.assertEqual(0, result["counts"]["probed"])

    def test_detect_also_refreshes_same_email_siblings_of_error_account(self):
        rows = [
            {"account_id": "50", "email": "d@example.com", "status": "error", "created_at": "2026-10-12T00:00:00Z"},
            {"account_id": "51", "email": "d@example.com", "status": "active", "created_at": "2026-10-14T00:00:00Z"},
            {"account_id": "60", "email": "e@example.com", "status": "active"},
        ]
        refreshed = []

        def refresh(ids):
            refreshed.append(sorted(ids))
            return {"total": len(ids), "success": 1, "failed": 1, "errors": [{"account_id": "50", "error": "401"}]}

        item = self.service(rows=rows, refresh=refresh).detect()["items"][0]
        self.assertEqual([["50", "51"]], refreshed)
        self.assertEqual("cleanup_ready", item["state"])
        self.assertEqual("51", item["replacement_account_id"])

    def test_in_flight_retry_refreshes_new_active_candidate(self):
        rows = [{"account_id": "old", "email": "one@example.com", "status": "error"}]
        refreshed = []

        def refresh(ids):
            refreshed.append(sorted(ids))
            errors = [{"account_id": "old", "error": "401"}] if "old" in ids else []
            return {"total": len(ids), "success": len(ids) - len(errors), "failed": len(errors), "errors": errors}

        service = self.service(rows=rows, refresh=refresh)
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com", "status": "active"})
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual(["new", "old"], refreshed[-1])
        self.assertEqual("cleanup_ready", item["state"])
        self.assertEqual("new", item["replacement_account_id"])

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

    def test_legacy_cleanup_ready_audit_defaults_replacement_health_to_healthy(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        deleted = []

        def refresh(ids):
            failures = [
                {"account_id": account_id, "error": "401 unauthorized"}
                for account_id in ids
                if account_id == "old"
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

        payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
        payload["items"]["old"].pop("replacement_health")
        self.audit_path.write_text(json.dumps(payload), encoding="utf-8")

        reloaded = self.service(rows=rows, refresh=refresh, delete=delete)
        self.assertEqual("healthy", reloaded.state()["items"][0]["replacement_health"])
        result = reloaded.cleanup(["old"], opt_in=True)

        self.assertEqual(["old"], deleted)
        self.assertTrue(result["items"][0]["ok"])
        self.assertEqual("cleaned", reloaded.state()["items"][0]["state"])

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

    def test_historical_all_unauthorized_duplicates_keep_unique_latest_out_of_items(self):
        rows = [
            {"account_id": "19", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]

        def refresh(ids):
            return {
                "total": len(ids),
                "success": 0,
                "failed": len(ids),
                "errors": [
                    {"account_id": account_id, "error": "401 unauthorized"}
                    for account_id in ids
                ],
            }

        result = self.service(rows=rows, refresh=refresh).detect()

        self.assertEqual(["19"], [item["account_id"] for item in result["items"]])
        item = result["items"][0]
        self.assertEqual("cleanup_ready", item["state"])
        self.assertEqual("22", item["replacement_account_id"])
        self.assertEqual("unauthorized", item["replacement_health"])
        self.assertEqual("19", item["cleanup_account_id"])
        self.assertIn("唯一最新失效账号为 keeper", item["reason"])

    def test_historical_all_unauthorized_duplicates_require_complete_unique_timestamps(self):
        cases = {
            "missing": [
                {"account_id": "19", "email": "one@example.com"},
                {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
            ],
            "invalid": [
                {"account_id": "19", "email": "one@example.com", "created_at": "not-a-date"},
                {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
            ],
            "tied": [
                {"account_id": "19", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
                {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
            ],
        }

        for name, rows in cases.items():
            with self.subTest(name=name):
                service = self.service(
                    rows=rows,
                    refresh=lambda ids: {
                        "total": len(ids),
                        "success": 0,
                        "failed": len(ids),
                        "errors": [
                            {"account_id": account_id, "error": "401 unauthorized"}
                            for account_id in ids
                        ],
                    },
                    audit_path=Path(self.temp_dir.name) / f"{name}.json",
                )
                result = service.detect()

                self.assertEqual({"19", "22"}, {item["account_id"] for item in result["items"]})
                self.assertTrue(all(item["state"] == "ambiguous" for item in result["items"]))
                self.assertTrue(all(not item["cleanup_account_id"] for item in result["items"]))

    def test_historical_duplicates_with_incomplete_health_remain_ambiguous(self):
        rows = [
            {"account_id": "19", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]

        for error in ("request timeout", "provider unavailable"):
            with self.subTest(error=error):
                result = self.service(
                    rows=rows,
                    refresh=lambda ids, message=error: {
                        "total": len(ids),
                        "success": 0,
                        "failed": len(ids),
                        "errors": [
                            {
                                "account_id": account_id,
                                "error": "401 unauthorized" if account_id == "19" else message,
                            }
                            for account_id in ids
                        ],
                    },
                    audit_path=Path(self.temp_dir.name) / f"health-{error.split()[0]}.json",
                ).detect()

                self.assertEqual({"19", "22"}, {item["account_id"] for item in result["items"]})
                self.assertTrue(all(item["state"] == "ambiguous" for item in result["items"]))
                self.assertTrue(all(not item["cleanup_account_id"] for item in result["items"]))

    def test_historical_all_unauthorized_cleanup_deletes_exact_old_id_then_exposes_keeper_pending(self):
        rows = [
            {"account_id": "19", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]
        deleted = []
        refresh_calls = []

        def refresh(ids):
            refresh_calls.append(list(ids))
            return {
                "total": len(ids),
                "success": 0,
                "failed": len(ids),
                "errors": [
                    {"account_id": account_id, "error": "401 unauthorized"}
                    for account_id in ids
                ],
            }

        def delete(account_id):
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            return {"ok": True}

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect()
        service = self.service(rows=rows, refresh=refresh, delete=delete)

        preview = service.cleanup(["19"], opt_in=False)
        self.assertFalse(preview["cleanup_performed"])
        self.assertEqual([], deleted)
        result = service.cleanup(["22", "19", "missing"], opt_in=True)

        self.assertEqual(["19"], deleted)
        self.assertTrue(result["items"][0]["ok"])
        self.assertEqual("cleaned", service.state()["items"][0]["state"])
        self.assertGreaterEqual(refresh_calls.count(["19", "22"]), 2)
        self.assertIn(["22"], refresh_calls)

        next_result = service.detect()
        self.assertEqual(["22"], [item["account_id"] for item in next_result["items"]])
        self.assertEqual("pending", next_result["items"][0]["state"])
        self.assertTrue(next_result["items"][0]["retryable"])

    def test_historical_all_unauthorized_cleanup_refuses_classification_drift(self):
        rows = [
            {"account_id": "19", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]
        drifted = False
        deleted = []

        def refresh(ids):
            unauthorized_ids = [
                account_id for account_id in ids
                if not drifted or account_id != "22"
            ]
            return {
                "total": len(ids),
                "success": len(ids) - len(unauthorized_ids),
                "failed": len(unauthorized_ids),
                "errors": [
                    {"account_id": account_id, "error": "401 unauthorized"}
                    for account_id in unauthorized_ids
                ],
            }

        service = self.service(
            rows=rows,
            refresh=refresh,
            delete=lambda account_id: deleted.append(account_id) or {"ok": True},
        )
        service.detect()
        drifted = True
        result = service.cleanup(["19"], opt_in=True)

        self.assertEqual([], deleted)
        self.assertFalse(result["items"][0]["ok"])
        item = service.state()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertIn("分类或唯一最新 keeper 已变化", item["reason"])

    def test_historical_all_unauthorized_cleanup_refuses_created_at_drift_before_delete(self):
        rows = [
            {"account_id": "19", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]
        deleted = []

        def refresh(ids):
            return {
                "total": len(ids),
                "success": 0,
                "failed": len(ids),
                "errors": [
                    {"account_id": account_id, "error": "401 unauthorized"}
                    for account_id in ids
                ],
            }

        service = self.service(
            rows=rows,
            refresh=refresh,
            delete=lambda account_id: deleted.append(account_id) or {"ok": True},
        )
        service.detect()
        rows[0]["created_at"] = "2026-10-12T00:00:00Z"
        result = service.cleanup(["19"], opt_in=True)

        self.assertEqual([], deleted)
        self.assertFalse(result["items"][0]["ok"])
        item = service.state()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertIn("created_at 快照已变化", item["reason"])

    def test_historical_all_unauthorized_cleanup_retains_post_delete_created_at_drift(self):
        rows = [
            {"account_id": "old-a", "email": "one@example.com", "created_at": "2026-10-10T00:00:00Z"},
            {"account_id": "old-b", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "keeper", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]
        deleted = []

        def refresh(ids):
            return {
                "total": len(ids),
                "success": 0,
                "failed": len(ids),
                "errors": [
                    {"account_id": account_id, "error": "401 unauthorized"}
                    for account_id in ids
                ],
            }

        def delete(account_id):
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            next(row for row in rows if row["account_id"] == "old-b")["created_at"] = "2026-10-12T00:00:00Z"
            return {"ok": True}

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect()
        result = service.cleanup(["old-a"], opt_in=True)

        self.assertEqual(["old-a"], deleted)
        self.assertFalse(result["items"][0]["ok"])
        item = {entry["account_id"]: entry for entry in service.state()["items"]}["old-a"]
        self.assertEqual("ambiguous", item["state"])
        self.assertIn("created_at 快照发生变化", item["reason"])

    def test_historical_all_unauthorized_cleanup_retains_post_delete_keeper_drift(self):
        rows = [
            {"account_id": "19", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "22", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]
        keeper_became_healthy = False
        deleted = []

        def refresh(ids):
            unauthorized_ids = [
                account_id for account_id in ids
                if not keeper_became_healthy or account_id != "22"
            ]
            return {
                "total": len(ids),
                "success": len(ids) - len(unauthorized_ids),
                "failed": len(unauthorized_ids),
                "errors": [
                    {"account_id": account_id, "error": "401 unauthorized"}
                    for account_id in unauthorized_ids
                ],
            }

        def delete(account_id):
            nonlocal keeper_became_healthy
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            keeper_became_healthy = True
            return {"ok": True}

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect()
        result = service.cleanup(["19"], opt_in=True)

        self.assertEqual(["19"], deleted)
        self.assertFalse(result["items"][0]["ok"])
        item = service.state()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertEqual("19", item["cleanup_account_id"])
        self.assertEqual("22", item["replacement_account_id"])
        self.assertEqual("unauthorized", item["replacement_health"])
        self.assertIn("未保持 unauthorized", item["reason"])

    def test_historical_all_unauthorized_cleanup_allows_sequential_ids_after_restart(self):
        rows = [
            {"account_id": "old-a", "email": "one@example.com", "created_at": "2026-10-11T00:00:00Z"},
            {"account_id": "old-b", "email": "one@example.com", "created_at": "2026-10-12T00:00:00Z"},
            {"account_id": "keeper", "email": "one@example.com", "created_at": "2026-10-13T00:00:00Z"},
        ]
        deleted = []

        def refresh(ids):
            return {
                "total": len(ids),
                "success": 0,
                "failed": len(ids),
                "errors": [
                    {"account_id": account_id, "error": "401 unauthorized"}
                    for account_id in ids
                ],
            }

        def delete(account_id):
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            return {"ok": True}

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect()
        first = service.cleanup(["old-a"], opt_in=True)
        self.assertTrue(first["items"][0]["ok"])

        reloaded = self.service(rows=rows, refresh=refresh, delete=delete)
        second = reloaded.cleanup(["old-b"], opt_in=True)

        self.assertTrue(second["items"][0]["ok"])
        self.assertEqual(["old-a", "old-b"], deleted)
        self.assertEqual(["keeper"], [row["account_id"] for row in rows])
        next_result = reloaded.detect()
        self.assertEqual(["keeper"], [item["account_id"] for item in next_result["items"]])
        self.assertEqual("pending", next_result["items"][0]["state"])
        self.assertTrue(next_result["items"][0]["retryable"])

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

    def test_old_account_missing_releases_new_id_and_blocks_cleanup(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        deleted = []
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": len(ids), "success": 0, "failed": len(ids), "errors": [{"account_id": value, "error": "401"} for value in ids]},
            delete=lambda account_id: deleted.append(account_id),
        )
        service.detect()
        service.mark_retrying(["old"])
        rows[:] = [{"account_id": "new", "email": "one@example.com"}]
        items = service.detect({"one@example.com": "success"})["items"]
        self.assertEqual(["new"], [item["account_id"] for item in items])
        self.assertEqual("pending", items[0]["state"])
        self.assertTrue(items[0]["retryable"])
        self.assertEqual("", items[0]["cleanup_account_id"])
        service.cleanup(["old"], opt_in=True)
        self.assertEqual([], deleted)

    def test_old_account_missing_keeps_lock_while_codex_retry_runs(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": len(ids), "success": 0, "failed": len(ids), "errors": [{"account_id": value, "error": "401"} for value in ids]},
        )
        service.detect()
        service.mark_retrying(["old"])
        rows[:] = [{"account_id": "new", "email": "one@example.com"}]
        items = service.detect({"one@example.com": "running"})["items"]
        self.assertEqual(["old"], [item["account_id"] for item in items])
        self.assertFalse(items[0]["retryable"])

    def test_cleaned_replacement_that_turns_401_becomes_its_own_pending_row(self):
        rows = [{"account_id": "old", "email": "one@example.com"}]
        failing = {"old"}

        def refresh(ids):
            errors = [{"account_id": value, "error": "Token revoked (401)"} for value in ids if value in failing]
            return {"total": len(ids), "success": len(ids) - len(errors), "failed": len(errors), "errors": errors}

        def delete(account_id):
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            return {"ok": True}

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect()
        service.mark_retrying(["old"])
        rows.append({"account_id": "new", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})
        service.cleanup(["old"], opt_in=True)
        self.assertEqual("cleaned", service.state()["items"][0]["state"])

        failing.add("new")
        by_id = {item["account_id"]: item for item in service.detect({"one@example.com": "success"})["items"]}
        self.assertEqual({"old", "new"}, set(by_id))
        self.assertEqual("cleaned", by_id["old"]["state"])
        self.assertEqual("pending", by_id["new"]["state"])
        self.assertEqual("unauthorized", by_id["new"]["health"])
        self.assertTrue(by_id["new"]["retryable"])

    def dead_service(self, rows, deleted, *, failing=("old",)):
        def refresh(ids):
            errors = [{"account_id": value, "error": "Token revoked (401)"} for value in ids if value in failing]
            return {"total": len(ids), "success": len(ids) - len(errors), "failed": len(errors), "errors": errors}

        def delete(account_id):
            deleted.append(account_id)
            rows[:] = [row for row in rows if row["account_id"] != account_id]
            return {"ok": True}

        return self.service(rows=rows, refresh=refresh, delete=delete)

    def test_dead_codex_retry_becomes_deactivated_then_deletes_exact_old_id(self):
        rows = [
            {"account_id": "old", "email": "one@example.com", "status": "error"},
            {"account_id": "other", "email": "two@example.com", "status": "active"},
        ]
        deleted = []
        service = self.dead_service(rows, deleted)
        service.detect()
        service.mark_retrying(["old"])
        item = service.detect({"one@example.com": "deactivated"})["items"][0]
        self.assertEqual("deactivated", item["state"])
        self.assertFalse(item["retryable"])
        self.assertEqual("old", item["cleanup_account_id"])

        preview = service.delete_deactivated(["old"])
        self.assertFalse(preview["deletion_performed"])
        self.assertEqual([], deleted)

        result = service.delete_deactivated(["old"], opt_in=True)
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual(["old"], deleted)
        self.assertEqual(["other"], [row["account_id"] for row in rows])
        self.assertEqual("deleted", service.state()["items"][0]["state"])

        items = service.detect({"one@example.com": "deactivated"})["items"]
        self.assertEqual([("old", "deleted")], [(entry["account_id"], entry["state"]) for entry in items])

    def test_dead_singleton_without_retry_is_deactivated_not_pending(self):
        rows = [{"account_id": "old", "email": "one@example.com", "status": "error"}]
        deleted = []
        service = self.dead_service(rows, deleted)
        item = service.detect({"one@example.com": "deactivated"})["items"][0]
        self.assertEqual("deactivated", item["state"])
        self.assertFalse(item["retryable"])
        self.assertEqual("old", item["cleanup_account_id"])
        service.delete_deactivated(["old"], opt_in=True)
        self.assertEqual(["old"], deleted)
        items = service.detect({"one@example.com": "deactivated"})["items"]
        self.assertEqual([("old", "deleted")], [(entry["account_id"], entry["state"]) for entry in items])

    def test_delete_deactivated_refuses_when_refresh_is_not_401(self):
        rows = [{"account_id": "old", "email": "one@example.com", "status": "error"}]
        deleted = []
        service = self.dead_service(rows, deleted)
        service.detect({"one@example.com": "deactivated"})
        recovered = self.dead_service(rows, deleted, failing=())
        result = recovered.delete_deactivated(["old"], opt_in=True)
        self.assertEqual([], deleted)
        self.assertEqual("ambiguous", result["items"][0]["state"])

    def test_delete_deactivated_failure_stays_deactivated_for_retry(self):
        rows = [{"account_id": "old", "email": "one@example.com", "status": "error"}]

        def refresh(ids):
            return {"total": len(ids), "success": 0, "failed": len(ids), "errors": [{"account_id": value, "error": "401"} for value in ids]}

        def delete(_account_id):
            raise RuntimeError("Sub2API 管理接口请求失败")

        service = self.service(rows=rows, refresh=refresh, delete=delete)
        service.detect({"one@example.com": "deactivated"})
        result = service.delete_deactivated(["old"], opt_in=True)
        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual("deactivated", service.state()["items"][0]["state"])

    def test_local_retry_status_marks_dead_accounts_and_keeps_running_retries(self):
        dead_error = (
            'RuntimeError: [Codex] MFA 验证失败 status=403: { "error": { "message": "You do not have an account '
            'because it has been deleted or deactivated.'
        )
        self.assertEqual("deactivated", local_retry_status({"codex_status": "failed", "codex_error": dead_error}))
        self.assertEqual("deactivated", local_retry_status({"codex_status": "failed", "live_check_status": "deactivated"}))
        self.assertEqual("deactivated", local_retry_status({"codex_status": "deactivated"}))
        self.assertEqual("running", local_retry_status({"codex_status": "running", "codex_error": dead_error}))
        self.assertEqual("failed", local_retry_status({"codex_status": "failed", "codex_error": "ProxyError: timeout"}))
        self.assertEqual("", local_retry_status({}))

    def test_stuck_audit_row_with_missing_old_id_releases_current_id(self):
        self.audit_path.write_text(json.dumps({"version": 2, "items": {"1": {
            "account_id": "1", "email": "one@example.com", "state": "ambiguous", "health": "unauthorized",
            "reason": "检测时锁定的旧 account_id 已变化或不存在，禁止清理",
            "pre_retry_account_ids": ["1"], "post_retry_account_ids": ["42"], "observed_account_ids": ["42"],
            "replacement_account_id": "42", "replacement_health": "healthy",
        }}}), encoding="utf-8")
        rows = [{"account_id": "42", "email": "one@example.com"}]
        service = self.service(
            rows=rows,
            refresh=lambda ids: {"total": 1, "success": 0, "failed": 1, "errors": [{"account_id": "42", "error": "Token revoked (401)"}]},
        )
        items = service.detect({"one@example.com": "success"})["items"]
        self.assertEqual(["42"], [item["account_id"] for item in items])
        self.assertEqual("pending", items[0]["state"])
        self.assertTrue(items[0]["retryable"])
        selected, skipped = service.select_retryable(["42"])
        self.assertEqual(["42"], [item["account_id"] for item in selected])
        self.assertEqual([], skipped)

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

    def test_cleanup_deactivation_failure_preserves_safe_retry_evidence(self):
        rows = [{"account_id": "42", "email": "one@example.com"}]
        errors = {"42": "401 unauthorized"}
        calls = []

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

        def request(method, path, body):
            calls.append((method, path, body))
            return {
                "code": 500,
                "message": "deactivation failed authorization=super-secret callback=https://secret.example/cb",
            }

        service = self.service(
            rows=rows,
            refresh=refresh,
            delete=Sub2ApiAdminClient(request).delete_account,
        )
        service.detect()
        service.mark_retrying(["42"])
        rows.append({"account_id": "84", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})

        result = service.cleanup(["42"], opt_in=True)

        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual([
            ("PUT", "/api/v1/admin/accounts/42", {"status": "inactive"}),
        ], calls)
        item = service.state()["items"][0]
        self.assertEqual("cleanup_ready", item["state"])
        self.assertEqual("42", item["cleanup_account_id"])
        self.assertEqual("84", item["replacement_account_id"])
        self.assertIn("deactivation failed", item["reason"])
        persisted = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn("super-secret", persisted)
        self.assertNotIn("secret.example", persisted)

    def test_cleanup_request_failure_stays_retryable_when_old_id_disappears_concurrently(self):
        rows = [{"account_id": "42", "email": "one@example.com"}]
        errors = {"42": "401 unauthorized"}
        calls = []

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

        def request(method, path, body):
            calls.append((method, path, body))
            rows[:] = [row for row in rows if row["account_id"] != "42"]
            return {"code": 500, "message": "deactivation failed"}

        service = self.service(
            rows=rows,
            refresh=refresh,
            delete=Sub2ApiAdminClient(request).delete_account,
        )
        service.detect()
        service.mark_retrying(["42"])
        rows.append({"account_id": "84", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})

        first = service.cleanup(["42"], opt_in=True)

        self.assertFalse(first["items"][0]["ok"])
        self.assertEqual("not_deleted", first["items"][0]["result"])
        self.assertEqual("cleanup_ready", service.state()["items"][0]["state"])
        self.assertEqual([
            ("PUT", "/api/v1/admin/accounts/42", {"status": "inactive"}),
        ], calls)

        second = service.cleanup(["42"], opt_in=True)

        self.assertTrue(second["items"][0]["ok"])
        self.assertEqual("absent_and_verified", second["items"][0]["result"])
        self.assertEqual("cleaned", service.state()["items"][0]["state"])
        self.assertEqual(1, len(calls))

    def test_cleanup_delete_failure_retries_deactivation_and_preserves_evidence(self):
        rows = [{"account_id": "42", "email": "one@example.com"}]
        errors = {"42": "401 unauthorized"}
        calls = []
        delete_attempts = 0

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

        def request(method, path, body):
            nonlocal delete_attempts
            calls.append((method, path, body))
            if method == "DELETE":
                delete_attempts += 1
                if delete_attempts == 1:
                    return {
                        "code": 500,
                        "message": "delete failed access_token=super-secret callback=https://secret.example/cb",
                    }
                rows[:] = [row for row in rows if row["account_id"] != "42"]
            return {"data": {"access_token": "must-not-return"}}

        service = self.service(
            rows=rows,
            refresh=refresh,
            delete=Sub2ApiAdminClient(request).delete_account,
        )
        service.detect()
        service.mark_retrying(["42"])
        rows.append({"account_id": "84", "email": "one@example.com"})
        service.detect({"one@example.com": "success"})

        first = service.cleanup(["42"], opt_in=True)

        self.assertFalse(first["items"][0]["ok"])
        self.assertEqual([
            ("PUT", "/api/v1/admin/accounts/42", {"status": "inactive"}),
            ("DELETE", "/api/v1/admin/accounts/42", None),
        ], calls)
        item = service.state()["items"][0]
        self.assertEqual("cleanup_ready", item["state"])
        self.assertEqual("42", item["cleanup_account_id"])
        self.assertEqual("84", item["replacement_account_id"])
        self.assertIn("delete failed", item["reason"])
        self.assertNotIn("super-secret", self.audit_path.read_text(encoding="utf-8"))

        second = service.cleanup(["42"], opt_in=True)

        self.assertTrue(second["items"][0]["ok"])
        self.assertEqual([
            ("PUT", "/api/v1/admin/accounts/42", {"status": "inactive"}),
            ("DELETE", "/api/v1/admin/accounts/42", None),
            ("PUT", "/api/v1/admin/accounts/42", {"status": "inactive"}),
            ("DELETE", "/api/v1/admin/accounts/42", None),
        ], calls)
        self.assertEqual("cleaned", service.state()["items"][0]["state"])

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
