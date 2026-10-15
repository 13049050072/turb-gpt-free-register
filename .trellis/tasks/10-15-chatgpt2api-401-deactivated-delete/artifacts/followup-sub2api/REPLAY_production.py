import collections, json, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from core.sub2api_401_service import Sub2Api401Service
snap = json.load(open(sys.argv[2], encoding="utf-8"))
rows = snap["rows"]
bad = {r["account_id"] for r in rows if r["status"] == "error"}
def refresh(ids):
    errors = [{"account_id": i, "error": "Token revoked (401)"} for i in ids if i in bad]
    return {"total": len(ids), "success": len(ids) - len(errors), "failed": len(errors), "errors": errors}
with tempfile.TemporaryDirectory() as tmp:
    audit = Path(tmp) / "a.json"; audit.write_text(json.dumps(snap["audit"], ensure_ascii=False), encoding="utf-8")
    statuses = {v["email"]: v.get("local_codex_status") or "" for v in snap["audit"]["items"].values()}
    result = Sub2Api401Service(list_fn=lambda: rows, refresh_fn=refresh, delete_fn=lambda i: {}, audit_path=audit).detect(statuses)
items = result["items"]
print("states:", dict(collections.Counter(i["state"] for i in items)))
print("pending retryable:", sorted((i["account_id"] for i in items if i["state"] == "pending" and i["retryable"]), key=int))
print("ambiguous rows:", sorted((i["account_id"] for i in items if i["state"] == "ambiguous"), key=int))
