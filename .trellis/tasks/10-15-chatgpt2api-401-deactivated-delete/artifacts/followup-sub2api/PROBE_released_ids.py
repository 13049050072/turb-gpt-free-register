# -*- coding: utf-8 -*-
"""Same-input probe replaying the production Sub2API row #1 -> #42.

Usage: python -I PROBE_released_ids.py <repo_root>
Audit holds the stuck historical row (old #1 gone, post-retry #42); Sub2API now
lists #42 whose refresh fails with Token revoked (401), plus one healthy #40.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from core.sub2api_401_service import Sub2Api401Service  # noqa: E402

stuck_row = {
    "account_id": "1", "email": "graven.grassy.4q@icloud.com", "state": "ambiguous", "health": "unauthorized",
    "reason": "检测时锁定的旧 account_id 已变化或不存在，禁止清理",
    "pre_retry_account_ids": ["1"], "post_retry_account_ids": ["42"], "observed_account_ids": ["42"],
    "replacement_account_id": "42", "replacement_health": "healthy", "local_codex_status": "success",
}
cleaned_row = {
    "account_id": "9", "email": "corsage.scalene0j@icloud.com", "state": "cleaned", "health": "unauthorized",
    "reason": "旧 Sub2API account_id 已删除，替代账号保持健康",
    "pre_retry_account_ids": ["9"], "post_retry_account_ids": ["40"], "observed_account_ids": ["40"],
    "replacement_account_id": "40", "replacement_health": "healthy", "cleanup_account_id": "9",
}
rows = [
    {"account_id": "40", "email": "corsage.scalene0j@icloud.com", "created_at": "2026-10-13T00:00:00Z"},
    {"account_id": "42", "email": "graven.grassy.4q@icloud.com", "created_at": "2026-10-13T00:00:00Z"},
]


def refresh(ids):
    errors = [{"account_id": "42", "error": "Token revoked (401): Encountered invalidated oauth token"}] if "42" in ids else []
    return {"total": len(ids), "success": len(ids) - len(errors), "failed": len(errors), "errors": errors}


with tempfile.TemporaryDirectory() as tmp:
    audit = Path(tmp) / "audit.json"
    audit.write_text(json.dumps({"version": 2, "items": {"1": stuck_row, "9": cleaned_row}}, ensure_ascii=False), encoding="utf-8")
    service = Sub2Api401Service(list_fn=lambda: list(rows), refresh_fn=refresh,
                                delete_fn=lambda account_id: {"ok": True}, audit_path=audit)
    result = service.detect({"graven.grassy.4q@icloud.com": "success", "corsage.scalene0j@icloud.com": "success"})
    print(json.dumps([
        {key: item[key] for key in ("account_id", "state", "health", "retryable", "reason")}
        for item in result["items"]
    ], ensure_ascii=False, sort_keys=True))
    print(json.dumps({"counts": {k: result["counts"].get(k) for k in ("pending", "ambiguous", "cleaned", "unauthorized")}}, sort_keys=True))
