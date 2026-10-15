# -*- coding: utf-8 -*-
"""Same-input probe: which Sub2API account IDs does detect() send to batch-refresh?

Usage: python -I PROBE_refresh_scope.py <repo_root>
Sub2API lists 5 accounts: #40/#41 active, #42 error (401), and one email with
#50 error plus a newer active sibling #51.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from core.sub2api_401_service import Sub2Api401Service  # noqa: E402

rows = [
    {"account_id": "40", "email": "a@example.com", "status": "active", "created_at": "2026-10-13T00:00:00Z"},
    {"account_id": "41", "email": "b@example.com", "status": "active", "created_at": "2026-10-13T00:00:00Z"},
    {"account_id": "42", "email": "c@example.com", "status": "error", "created_at": "2026-10-13T00:00:00Z"},
    {"account_id": "50", "email": "d@example.com", "status": "error", "created_at": "2026-10-12T00:00:00Z"},
    {"account_id": "51", "email": "d@example.com", "status": "active", "created_at": "2026-10-14T00:00:00Z"},
]
revoked = {"42", "50"}
refreshed = []


def refresh(ids):
    refreshed.append(sorted(ids, key=int))
    errors = [{"account_id": i, "error": "Token revoked (401)"} for i in ids if i in revoked]
    return {"total": len(ids), "success": len(ids) - len(errors), "failed": len(errors), "errors": errors}


with tempfile.TemporaryDirectory() as tmp:
    service = Sub2Api401Service(list_fn=lambda: list(rows), refresh_fn=refresh,
                                delete_fn=lambda account_id: {"ok": True}, audit_path=Path(tmp) / "a.json")
    result = service.detect({})
counts = result["counts"]
print(json.dumps({
    "refresh_calls": refreshed,
    "items": [{k: i[k] for k in ("account_id", "state", "health", "retryable", "replacement_account_id")} for i in result["items"]],
    "health_counts": {k: counts.get(k) for k in ("healthy", "unauthorized", "timeout", "unknown")},
    "probed": counts.get("probed"),
}, ensure_ascii=False, sort_keys=True))
