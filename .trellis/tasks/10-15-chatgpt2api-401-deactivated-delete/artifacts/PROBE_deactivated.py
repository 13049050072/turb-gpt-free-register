# -*- coding: utf-8 -*-
"""Same-input probe: one remote 401 row whose local liveness reports account_deactivated.

Usage: python -I PROBE_deactivated.py <repo_root>
Prints the terminal audit state, delete calls, and remaining remote IDs.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from core.chatgpt2api_401_service import ChatGPT2API401Service  # noqa: E402

remote = [
    {"remote_id": "remote-dead", "email": "dead@example.com", "invalid": True,
     "reason": "access token invalid", "access_token": "old-at", "refresh_token": "rt"},
    {"remote_id": "remote-ok", "email": "ok@example.com", "invalid": False,
     "access_token": "ok-at", "refresh_token": "rt2"},
]
local = [{"id": 7, "email": "dead@example.com", "access_token": "local-at"}]
deletes, patches, liveness_calls = [], [], []


def snapshot(remote_id):
    return dict(next(row for row in remote if row["remote_id"] == remote_id))


def patch(remote_id, token):
    patches.append(remote_id)
    return {"ok": True}


def delete(remote_id):
    deletes.append(remote_id)
    remote[:] = [row for row in remote if row["remote_id"] != remote_id]
    return {"ok": True, "remote_id": remote_id}


def liveness(local_id):
    liveness_calls.append(local_id)
    local[0]["live_check_status"] = "deactivated"
    return {"ok": False, "status": "deactivated", "error": "account_deactivated"}


with tempfile.TemporaryDirectory() as tmp:
    kwargs = dict(
        list_remote_fn=lambda: list(remote), snapshot_fn=snapshot, patch_fn=patch,
        list_local_fn=lambda: list(local), liveness_fn=liveness,
        audit_path=Path(tmp) / "audit.json",
    )
    try:
        service = ChatGPT2API401Service(delete_fn=delete, **kwargs)
        delete_supported = True
    except TypeError:
        service = ChatGPT2API401Service(**kwargs)
        delete_supported = False
    service.detect()
    service.refresh(["remote-dead"], opt_in=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = service.state()
        if all(item["state"] not in {"queued", "refreshing", "ready"} for item in state["items"]):
            break
        time.sleep(0.02)
    item = state["items"][0]
    print(json.dumps({
        "delete_fn_supported": delete_supported,
        "state": item["state"],
        "failure_stage": item["failure_stage"],
        "retryable": item["retryable"],
        "reason": item["reason"],
        "liveness_calls": liveness_calls,
        "delete_calls": deletes,
        "patch_calls": patches,
        "remote_ids_left": [row["remote_id"] for row in remote],
        "progress_completed": state["progress"]["completed"],
        "progress_failed": state["progress"]["failed"],
    }, ensure_ascii=False, sort_keys=True))
