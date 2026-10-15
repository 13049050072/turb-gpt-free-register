# -*- coding: utf-8 -*-
"""Replay sanitized production Sub2API list + audit + local Codex statuses.

Usage: python -I REPLAY_production.py <repo_root> <snapshot.json>
Refresh is simulated from Sub2API status=error; delete is a fake that records IDs.
"""
import collections, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
import core.sub2api_401_service as svc
snap = json.load(open(sys.argv[2], encoding="utf-8"))
rows = list(snap["rows"])
bad = {r["account_id"] for r in rows if r["status"] == "error"}
deleted = []
def refresh(ids):
    errors = [{"account_id": i, "error": "Token revoked (401)"} for i in ids if i in bad]
    return {"total": len(ids), "success": len(ids) - len(errors), "failed": len(errors), "errors": errors}
def delete(account_id):
    deleted.append(account_id); rows[:] = [r for r in rows if r["account_id"] != account_id]; return {"ok": True}
status_fn = getattr(svc, "local_retry_status", lambda a: str(a.get("codex_status") or "").lower())
statuses = {a["email"].lower(): status_fn(a) for a in snap["local"]}
with tempfile.TemporaryDirectory() as tmp:
    audit = Path(tmp) / "a.json"; audit.write_text(json.dumps(snap["audit"], ensure_ascii=False), encoding="utf-8")
    service = svc.Sub2Api401Service(list_fn=lambda: list(rows), refresh_fn=refresh, delete_fn=delete, audit_path=audit)
    items = service.detect(statuses)["items"]
    print("after detect:", dict(collections.Counter(i["state"] for i in items)))
    dead = sorted((i["account_id"] for i in items if i["state"] == "deactivated"), key=int)
    print("deactivated:", dead)
    if hasattr(service, "delete_deactivated") and dead:
        service.delete_deactivated(dead, opt_in=True)
        print("after delete:", dict(collections.Counter(i["state"] for i in service.state()["items"])))
print("delete calls:", deleted, "remaining listed:", len(rows))
