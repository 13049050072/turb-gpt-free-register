import json
import tempfile
import unittest
from pathlib import Path

from core.cpa_401_service import Cpa401Service, CpaRefreshUnsupported, CpaSnapshotConflict


class Cpa401ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.audit_path = Path(self.temp_dir.name) / "cpa-401-audit.json"

    def service(self, *, rows, refresh=None, download=None, cleanup_adapter=None):
        return Cpa401Service(
            refresh_fn=refresh or (lambda: {"results": []}),
            list_fn=lambda: list(rows),
            download_fn=download or (lambda **kwargs: ('{"type":"codex"}', kwargs.get("cpa_name", ""), {})),
            cleanup_adapter=cleanup_adapter,
            audit_path=self.audit_path,
        )

    def test_refresh_filters_unauthorized_and_excludes_healthy(self):
        rows = [
            {"id": "bad", "provider": "codex", "name": "codex-bad.json", "email": "Bad@Example.com", "status": "error"},
            {"id": "ok", "provider": "codex", "name": "codex-ok.json", "email": "ok@example.com", "status": "active"},
        ]
        service = self.service(
            rows=rows,
            refresh=lambda: {"results": [{"id": "bad", "success": False, "error": "401 unauthorized"}]},
        )
        result = service.detect({"bad@example.com": "failed"})
        self.assertEqual(["bad@example.com"], [row["email"] for row in result["items"]])
        self.assertEqual("pending", result["items"][0]["state"])
        self.assertTrue(result["items"][0]["retryable"])
        self.assertEqual(1, result["counts"]["unauthorized"])
        self.assertEqual(1, result["counts"]["healthy"])

    def test_pre_retry_snapshot_persists_across_service_restart(self):
        rows = [{"id": "one", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        service.mark_retrying(["ONE@example.com"], batch_id="batch-1")

        payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
        self.assertEqual(["old.json"], payload["items"]["one@example.com"]["pre_retry_files"])
        reloaded = self.service(rows=rows).state()["items"][0]
        self.assertEqual("retrying", reloaded["state"])
        self.assertEqual("batch-1", reloaded["retry_batch_id"])
        self.assertEqual(["old.json"], reloaded["pre_retry_files"])
        self.assertTrue(reloaded["pre_retry_snapshot_at"])

    def test_unique_new_verified_replacement_becomes_cleanup_ready(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        downloaded = []
        service = self.service(
            rows=rows,
            download=lambda **kwargs: downloaded.append(kwargs["cpa_name"]) or ('{"type":"codex","email":"one@example.com"}', kwargs["cpa_name"], {"email": "one@example.com"}),
        )
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows[:] = [
            {"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"},
            {"id": "new", "name": "new.json", "email": "ONE@example.com", "status": "active"},
        ]
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("cleanup_ready", item["state"])
        self.assertEqual("new.json", item["replacement_file"])
        self.assertEqual(["old.json"], item["pre_retry_files"])
        self.assertEqual(["new.json", "old.json"], item["post_retry_files"])
        self.assertTrue(item["post_retry_snapshot_at"])
        self.assertEqual(["old.json"], item["cleanup_files"])
        self.assertEqual(["new.json"], downloaded)
        self.assertFalse(item["cleanup_supported"])

    def test_same_filename_overwrite_is_verified_without_cleanup(self):
        rows = [{"id": "one", "name": "same.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows[0] = {**rows[0], "status": "active", "status_message": ""}
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("verified", item["state"])
        self.assertEqual("same.json", item["replacement_file"])
        self.assertEqual([], item["cleanup_files"])

    def test_retry_failure_preserves_old_file_and_can_retry(self):
        rows = [{"id": "one", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        service.mark_retrying(["one@example.com"])
        item = service.detect({"one@example.com": "failed"})["items"][0]
        self.assertEqual("failed", item["state"])
        self.assertEqual(["old.json"], item["pre_retry_files"])
        self.assertEqual(["old.json"], item["files"])
        self.assertTrue(item["retryable"])

    def test_live_pre_retry_snapshot_change_aborts_before_oauth(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        rows[:] = [{"id": "changed", "name": "changed.json", "email": "one@example.com", "status_message": "401"}]
        with self.assertRaises(CpaSnapshotConflict) as captured:
            service.mark_retrying(["one@example.com"], batch_id="batch-stale")
        self.assertEqual(["one@example.com"], captured.exception.emails)
        item = service.state()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertEqual(["changed.json"], item["files"])
        self.assertEqual([], item["pre_retry_files"])
        self.assertEqual([], item["cleanup_files"])

    def test_post_retry_snapshot_persists_when_refresh_fails(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        refresh_error = {"enabled": False}

        def refresh():
            if refresh_error["enabled"]:
                raise RuntimeError("request timeout access_token=not-persisted")
            return {"results": []}

        service = self.service(rows=rows, refresh=refresh)
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows.append({"id": "new", "name": "new.json", "email": "one@example.com", "status": "active"})
        refresh_error["enabled"] = True
        with self.assertRaises(RuntimeError):
            service.detect({"one@example.com": "success"})
        reloaded = self.service(rows=rows).state()["items"][0]
        self.assertEqual("ambiguous", reloaded["state"])
        self.assertEqual(["new.json", "old.json"], reloaded["post_retry_files"])
        self.assertTrue(reloaded["post_retry_snapshot_at"])
        self.assertEqual([], reloaded["cleanup_files"])
        self.assertNotIn("not-persisted", reloaded["reason"])

    def test_duplicate_same_name_candidate_is_not_unique(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows[:] = [
            {"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"},
            {"id": "new-a", "name": "new.json", "email": "one@example.com", "status": "active"},
            {"id": "new-b", "name": "new.json", "email": "one@example.com", "status": "active"},
        ]
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertEqual("", item["replacement_file"])
        self.assertEqual([], item["cleanup_files"])

    def test_download_identity_mismatch_preserves_old_file(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(
            rows=rows,
            download=lambda **kwargs: ('{"type":"codex","email":"other@example.com"}', kwargs["cpa_name"], {"email": "other@example.com"}),
        )
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows.append({"id": "new", "name": "new.json", "email": "one@example.com", "status": "active"})
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("failed", item["state"])
        self.assertEqual(["old.json"], item["pre_retry_files"])
        self.assertEqual([], item["cleanup_files"])
        self.assertEqual("", item["replacement_file"])

    def test_multiple_new_candidates_are_ambiguous(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows[:] = [
            {"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"},
            {"id": "new-a", "name": "new-a.json", "email": "one@example.com", "status": "active"},
            {"id": "new-b", "name": "new-b.json", "email": "one@example.com", "status": "active"},
        ]
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertEqual([], item["cleanup_files"])

    def test_missing_post_retry_file_is_ambiguous(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows.clear()
        item = service.detect({"one@example.com": "success"})["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertEqual(["old.json"], item["pre_retry_files"])
        self.assertFalse(item["retryable"])

    def test_duplicate_initial_credentials_are_ambiguous_and_not_retryable(self):
        rows = [
            {"id": "a", "name": "a.json", "email": "dup@example.com", "status_message": "401"},
            {"id": "b", "name": "b.json", "email": "DUP@example.com", "status_message": "401"},
        ]
        item = self.service(rows=rows).detect()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertFalse(item["retryable"])
        self.assertEqual(2, item["candidate_count"])

    def test_selection_deduplicates_and_rejects_invalid_or_untracked(self):
        rows = [{"id": "a", "name": "a.json", "email": "a@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        selected, skipped = service.select_retryable(["A@example.com", "a@example.com", "bad", "missing@example.com"])
        self.assertEqual(["a@example.com"], [row["email"] for row in selected])
        self.assertEqual(2, len(skipped))

    def test_cleanup_without_adapter_is_review_only(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows.append({"id": "new", "name": "new.json", "email": "one@example.com", "status": "active"})
        service.detect({"one@example.com": "success"})
        result = service.cleanup(["one@example.com"], opt_in=True)
        self.assertFalse(result["cleanup_supported"])
        self.assertFalse(result["cleanup_performed"])
        self.assertEqual(["old.json"], result["items"][0]["cleanup_files"])
        self.assertEqual("cleanup_ready", service.state()["items"][0]["state"])

    def test_cleanup_adapter_is_exact_idempotent_and_reports_failure(self):
        rows = [{"id": "old", "name": "old.json", "email": "one@example.com", "status_message": "401"}]
        deleted = []
        service = self.service(rows=rows, cleanup_adapter=lambda name: deleted.append(name) or False)
        service.detect()
        service.mark_retrying(["one@example.com"])
        rows.append({"id": "new", "name": "new.json", "email": "one@example.com", "status": "active"})
        service.detect({"one@example.com": "success"})
        failed = service.cleanup(["one@example.com"], opt_in=True)
        self.assertFalse(failed["items"][0]["ok"])
        self.assertEqual(["old.json"], deleted)
        self.assertEqual("cleanup_ready", service.state()["items"][0]["state"])

        deleted.clear()
        rows[:] = [row for row in rows if row["name"] != "old.json"]
        succeeded = service.cleanup(["one@example.com"], opt_in=True)
        self.assertTrue(succeeded["items"][0]["ok"])
        self.assertEqual([], deleted)
        self.assertEqual("already_absent", succeeded["items"][0]["files"][0]["result"])
        self.assertEqual("cleaned", service.state()["items"][0]["state"])

    def test_status_and_reason_fields_are_bounded(self):
        rows = [{"id": "a", "name": "a.json", "email": "a@example.com", "status_message": "401"}]
        service = self.service(rows=rows)
        item = service.detect({"a@example.com": "running-" + ("x" * 200)})["items"][0]
        self.assertLessEqual(len(item["local_codex_status"]), 64)
        service.mark_retrying(["a@example.com"])
        service.mark_verification_error(
            ["a@example.com"],
            ("verification failed " * 30) + " access_token=fixture-value",
        )
        item = service.state()["items"][0]
        self.assertLessEqual(len(item["reason"]), 180)
        self.assertNotIn("fixture-value", item["reason"])
        self.assertIn(item["state"], {"pending", "retrying", "verified", "cleanup_ready", "cleaned", "failed", "ambiguous"})

    def test_reason_redacts_sensitive_values(self):
        rows = [{"id": "a", "name": "a.json", "email": "a@example.com", "status": "error"}]
        service = self.service(
            rows=rows,
            refresh=lambda: {"results": [{"id": "a", "success": False, "error": "401 access_token=super-secret refresh_token=other"}]},
        )
        reason = service.detect()["items"][0]["reason"]
        self.assertNotIn("super-secret", reason)
        self.assertNotIn("other", reason)
        self.assertEqual("CPA 返回 unauthorized/401", reason)

    def test_404_refresh_is_explicitly_unsupported(self):
        def fail():
            raise RuntimeError("status=404 body=not_found")

        with self.assertRaises(CpaRefreshUnsupported):
            self.service(rows=[], refresh=fail).detect()


if __name__ == "__main__":
    unittest.main()
