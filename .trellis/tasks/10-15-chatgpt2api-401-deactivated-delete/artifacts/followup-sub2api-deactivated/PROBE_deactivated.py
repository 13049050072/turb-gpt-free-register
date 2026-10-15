# -*- coding: utf-8 -*-
"""Same-input probe replaying production Sub2API #42 after a failed Codex retry.

Usage: python -I PROBE_deactivated.py <repo_root>
Local account: codex_status=failed with the real MFA 403 "deleted or deactivated"
text. Audit: retry row #42 (failed). Sub2API: #42 status=error, refresh 401.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
import core.sub2api_401_service as svc  # noqa: E402
from core.openai_auth import detect_account_unusable_text  # noqa: E402

codex_error = (
    'RuntimeError: [Codex] MFA 验证失败 status=403: { "error": { "message": "You do not have an account '
    'because it has been deleted or deactivated. If you believe this was an error, please contact us through our help'
)
local_account = {"email": "graven.grassy.4q@icloud.com", "codex_status": "failed",
                 "codex_error": codex_error, "live_check_status": "live"}
row = {"account_id": "42", "email": "graven.grassy.4q@icloud.com", "state": "failed", "health": "unauthorized",
       "reason": "Codex 补跑失败；旧 Sub2API account_id 保持不变", "pre_retry_account_ids": ["42"],
       "post_retry_account_ids": ["42"], "observed_account_ids": ["42"], "local_codex_status": "failed"}
rows = [{"account_id": "42", "email": "graven.grassy.4q@icloud.com", "status": "error"},
        {"account_id": "40", "email": "ok@example.com", "status": "active"}]
deleted = []


def refresh(ids):
    errors = [{"account_id": i, "error": "Token revoked (401)"} for i in ids if i == "42"]
    return {"total": len(ids), "success": len(ids) - len(errors), "failed": len(errors), "errors": errors}


def delete(account_id):
    deleted.append(account_id)
    rows[:] = [r for r in rows if r["account_id"] != account_id]
    return {"ok": True}


status_fn = getattr(svc, "local_retry_status", None)
local_status = status_fn(local_account) if status_fn else local_account["codex_status"]
out = {"detector": detect_account_unusable_text(codex_error), "local_status": local_status}
with tempfile.TemporaryDirectory() as tmp:
    audit = Path(tmp) / "a.json"
    audit.write_text(json.dumps({"version": 2, "items": {"42": row}}), encoding="utf-8")
    service = svc.Sub2Api401Service(list_fn=lambda: list(rows), refresh_fn=refresh, delete_fn=delete, audit_path=audit)
    item = service.detect({local_account["email"]: local_status})["items"][0]
    out["after_detect"] = {k: item[k] for k in ("account_id", "state", "retryable", "reason")}
    if hasattr(service, "delete_deactivated"):
        service.delete_deactivated(["42"], opt_in=True)
        final = service.state()["items"][0]
        out["after_delete"] = {k: final[k] for k in ("account_id", "state", "reason")}
out["delete_calls"] = deleted
out["remote_ids_left"] = [r["account_id"] for r in rows]
print(json.dumps(out, ensure_ascii=False, sort_keys=True))
