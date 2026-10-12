# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path

from core.chatgpt2api_401_service import ChatGPT2API401Service


class ChatGPT2API401ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.audit_path = Path(self.temp_dir.name) / "chatgpt2api_401_audit.json"
        self.remote = [{
            "remote_id": "remote-old",
            "email": "one@example.com",
            "invalid": True,
            "reason": "HTTP 401 unauthorized",
            "access_token": "remote-old-secret",
            "password": "password-secret",
            "refresh_token": "refresh-secret",
            "client_id": "client-secret",
            "last_refresh": "2026-10-12T00:00:00Z",
        }]
        self.local = [{
            "id": 7,
            "email": "one@example.com",
            "access_token": "local-old-secret",
        }]
        self.patches = []
        self.liveness_calls = []

    def snapshot(self, remote_id):
        row = next(row for row in self.remote if row["remote_id"] == remote_id)
        return dict(row)

    def patch_remote(self, remote_id, access_token):
        self.patches.append((remote_id, access_token))
        row = next(row for row in self.remote if row["remote_id"] == remote_id)
        row["access_token"] = access_token
        return {"ok": True}

    def liveness(self, local_id):
        self.liveness_calls.append(local_id)
        self.local[0]["access_token"] = "local-new-secret"
        return {"ok": True, "access_token": "local-new-secret"}

    def service(self, **overrides):
        values = {
            "list_remote_fn": lambda: list(self.remote),
            "snapshot_fn": self.snapshot,
            "patch_fn": self.patch_remote,
            "list_local_fn": lambda: list(self.local),
            "liveness_fn": self.liveness,
            "audit_path": self.audit_path,
        }
        values.update(overrides)
        return ChatGPT2API401Service(**values)

    def test_full_flow_refreshes_locally_then_patches_only_locked_id_and_verifies(self):
        service = self.service()
        detected = service.detect()
        item = detected["items"][0]
        self.assertEqual("pending", item["state"])
        self.assertTrue(item["retryable"])
        self.assertEqual("remote-old", item["remote_id"])
        self.assertNotEqual(item["old_at_fingerprint"], "")

        refreshed = service.refresh(["remote-old"])
        self.assertEqual(1, refreshed["refreshed_count"])
        self.assertEqual([7], self.liveness_calls)
        ready = service.state()["items"][0]
        self.assertEqual("ready", ready["state"])
        self.assertNotEqual(ready["old_at_fingerprint"], ready["new_at_fingerprint"])
        self.assertEqual([], self.patches)

        preview = service.replace(["remote-old"], opt_in=False)
        self.assertFalse(preview["replacement_performed"])
        self.assertEqual([], self.patches)

        before = {key: self.remote[0][key] for key in ("email", "password", "refresh_token", "client_id", "last_refresh")}
        replaced = service.replace(["remote-old"], opt_in=True)
        self.assertEqual(1, replaced["replaced_count"])
        self.assertEqual([("remote-old", "local-new-secret")], self.patches)
        self.assertEqual("local-new-secret", self.remote[0]["access_token"])
        self.assertEqual(before, {key: self.remote[0][key] for key in before})
        final = service.state()["items"][0]
        self.assertEqual("replaced", final["state"])
        self.assertIn("旧 AT 摘要已变化", final["reason"])

    def test_ready_state_survives_restart_and_can_be_explicitly_replaced(self):
        service = self.service()
        service.detect()
        service.refresh(["remote-old"])

        reloaded = self.service()
        ready = reloaded.state()["items"][0]
        self.assertEqual("ready", ready["state"])
        self.assertTrue(ready["replaceable"])
        result = reloaded.replace(["remote-old"], opt_in=True)
        self.assertEqual(1, result["replaced_count"])
        self.assertEqual([("remote-old", "local-new-secret")], self.patches)

    def test_ambiguous_remote_or_local_email_never_runs_liveness_or_patch(self):
        self.remote.append({
            **self.remote[0],
            "remote_id": "remote-other",
            "access_token": "other-secret",
        })
        service = self.service()
        detected = service.detect()
        self.assertTrue(all(item["state"] == "ambiguous" for item in detected["items"]))
        self.assertEqual([], service.refresh(["remote-old"])["items"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], service.replace(["remote-old"], opt_in=True)["items"])
        self.assertEqual([], self.patches)

    def test_remote_row_without_id_still_blocks_unique_email_mapping(self):
        self.remote.append({
            "remote_id": "",
            "email": "one@example.com",
            "invalid": False,
            "reason": "",
        })
        snapshots = []
        service = self.service(snapshot_fn=lambda remote_id: snapshots.append(remote_id) or self.snapshot(remote_id))
        item = service.detect()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertIn("多个远端账号", item["reason"])
        self.assertEqual([], snapshots)

    def test_duplicate_stable_remote_id_is_ambiguous_and_never_snapshotted(self):
        self.remote.append({
            **self.remote[0],
            "email": "two@example.com",
            "access_token": "duplicate-secret",
        })
        snapshots = []
        service = self.service(snapshot_fn=lambda remote_id: snapshots.append(remote_id) or {})
        item = service.detect()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertIn("ID", item["reason"])
        self.assertEqual([], snapshots)
        service.refresh(["remote-old"])
        service.replace(["remote-old"], opt_in=True)
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_unmatched_email_never_runs_liveness_or_patch(self):
        self.local[0]["email"] = "other@example.com"
        service = self.service()
        item = service.detect()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertFalse(item["retryable"])
        service.refresh(["remote-old"])
        service.replace(["remote-old"], opt_in=True)
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_generic_remote_error_is_not_a_candidate(self):
        self.remote[0].update({"invalid": False, "reason": "generic upstream error"})
        result = self.service().detect()
        self.assertEqual([], result["items"])
        self.assertEqual(0, result["counts"]["invalid"])

    def test_remote_snapshot_change_before_refresh_blocks_liveness(self):
        service = self.service()
        service.detect()
        self.remote[0]["access_token"] = "changed-by-other-worker"
        result = service.refresh(["remote-old"])
        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual("conflict", service.state()["items"][0]["state"])
        self.assertEqual([], self.liveness_calls)
        self.assertEqual([], self.patches)

    def test_liveness_must_persist_a_new_local_token(self):
        service = self.service(liveness_fn=lambda _local_id: {"ok": True, "access_token": "not-persisted"})
        service.detect()
        result = service.refresh(["remote-old"])
        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual("failed", service.state()["items"][0]["state"])
        self.assertEqual([], self.patches)

    def test_liveness_returned_token_must_match_persisted_token(self):
        def mismatched_liveness(_local_id):
            self.local[0]["access_token"] = "persisted-token"
            return {"ok": True, "access_token": "returned-token"}

        service = self.service(liveness_fn=mismatched_liveness)
        service.detect()
        result = service.refresh(["remote-old"])
        self.assertFalse(result["items"][0]["ok"])
        self.assertIn("不一致", result["items"][0]["reason"])
        self.assertEqual([], self.patches)

    def test_incomplete_preserved_field_snapshot_is_never_actionable(self):
        def incomplete_snapshot(remote_id):
            row = self.snapshot(remote_id)
            row.pop("refresh_token")
            return row

        service = self.service(snapshot_fn=incomplete_snapshot)
        item = service.detect()["items"][0]
        self.assertEqual("ambiguous", item["state"])
        self.assertFalse(item["retryable"])
        self.assertIn("保留字段", item["reason"])
        self.assertEqual([], service.refresh(["remote-old"])["items"])
        self.assertEqual([], self.liveness_calls)

    def test_interrupted_refresh_is_recovered_as_retryable_failure(self):
        service = self.service()
        service.detect()
        payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
        payload["items"]["remote-old"].update({
            "state": "refreshing",
            "retryable": False,
            "replaceable": False,
        })
        self.audit_path.write_text(json.dumps(payload), encoding="utf-8")

        reloaded = self.service()
        item = reloaded.state()["items"][0]
        self.assertEqual("failed", item["state"])
        self.assertTrue(item["retryable"])
        self.assertIn("中断", item["reason"])

    def test_shared_audit_lock_reloads_state_and_rejects_stale_second_refresh(self):
        first = self.service()
        first.detect()
        stale = self.service()

        result = first.refresh(["remote-old"])
        self.assertEqual(1, result["refreshed_count"])
        stale_result = stale.refresh(["remote-old"])
        self.assertEqual([], stale_result["items"])
        self.assertEqual([7], self.liveness_calls)

    def test_equal_locked_old_and_new_hash_is_rejected_before_patch(self):
        service = self.service()
        service.detect()
        service.refresh(["remote-old"])
        payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
        payload["items"]["remote-old"]["new_at_sha256"] = payload["items"]["remote-old"]["old_at_sha256"]
        self.audit_path.write_text(json.dumps(payload), encoding="utf-8")
        self.local[0]["access_token"] = "remote-old-secret"

        result = service.replace(["remote-old"], opt_in=True)
        self.assertFalse(result["replacement_performed"])
        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual([], self.patches)

    def test_local_token_change_after_ready_blocks_patch(self):
        service = self.service()
        service.detect()
        service.refresh(["remote-old"])
        self.local[0]["access_token"] = "changed-after-ready"
        result = service.replace(["remote-old"], opt_in=True)
        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual("conflict", service.state()["items"][0]["state"])
        self.assertEqual([], self.patches)

    def test_patch_post_verification_rejects_preserved_field_change(self):
        def bad_patch(remote_id, access_token):
            self.patch_remote(remote_id, access_token)
            self.remote[0]["refresh_token"] = "mutated-refresh-token"

        service = self.service(patch_fn=bad_patch)
        service.detect()
        service.refresh(["remote-old"])
        result = service.replace(["remote-old"], opt_in=True)
        self.assertFalse(result["items"][0]["ok"])
        self.assertEqual("conflict", service.state()["items"][0]["state"])

    def test_unknown_selected_id_is_redacted_in_skipped_response(self):
        service = self.service()
        service.detect()
        result = service.refresh(["access_token=top-secret callback=https://example.test/cb?code=secret"])
        serialized = json.dumps(result)
        self.assertIn("[redacted]", serialized)
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("https://example.test", serialized)

    def test_audit_is_atomic_private_symlink_safe_and_redacted(self):
        target_dir = Path(self.temp_dir.name) / "data"
        target_dir.mkdir()
        target = target_dir / "audit.json"
        link = Path(self.temp_dir.name) / "audit-link.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unsupported")
        self.remote[0]["reason"] = "HTTP 401 access_token=remote-old-secret callback=https://example.test/cb?code=secret"
        service = self.service(audit_path=link)
        service.detect()
        self.assertTrue(link.is_symlink())
        payload = target.read_text(encoding="utf-8")
        for secret in (
            "remote-old-secret", "local-old-secret", "password-secret", "refresh-secret",
            "client-secret", "https://example.test", "code=secret",
        ):
            self.assertNotIn(secret, payload)
        parsed = json.loads(payload)
        self.assertEqual("pending", parsed["items"]["remote-old"]["state"])
        if os.name != "nt":
            self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertEqual([], list(target_dir.glob("*.tmp")))

    def test_corrupt_audit_counts_are_safely_bounded(self):
        self.audit_path.write_text(json.dumps({
            "remote_counts": {"total": "not-a-number", "invalid": -3, "healthy": 9_999_999},
            "items": {},
        }), encoding="utf-8")
        state = self.service().state()
        self.assertEqual(0, state["counts"]["total"])
        self.assertEqual(0, state["counts"]["invalid"])
        self.assertEqual(1_000_000, state["counts"]["healthy"])


if __name__ == "__main__":
    unittest.main()
