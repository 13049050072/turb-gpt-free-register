# -*- coding: utf-8 -*-
"""Same-input browser probe for the Sub2API 401 page state filter and dead-account selection.

Usage: python -I PROBE_filter.py <repo_root>
Mocked state: 2 deleted, 1 deactivated, 2 pending, 1 failed (retryable), 1 cleaned, 1 ambiguous.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

html = (Path(sys.argv[1]) / "webui/templates/sub2api_401.html").read_text(encoding="utf-8")


def item(n, state, retryable=False):
    return {"account_id": str(n), "email": f"u{n}@example.com", "account_name": "", "state": state, "health": "unauthorized",
            "reason": state, "pre_retry_account_ids": [], "observed_account_ids": [str(n)], "post_retry_account_ids": [],
            "replacement_account_id": "", "cleanup_account_id": str(n) if state == "deactivated" else "",
            "local_codex_status": "", "retryable": retryable}


items = [item(1, "deleted"), item(2, "deleted"), item(3, "deactivated"), item(4, "pending", True),
         item(5, "pending", True), item(6, "failed", True), item(7, "cleaned"), item(8, "ambiguous")]
state = {"ok": True, "items": items, "cleanup_supported": True, "counts": {"deleted": 2, "deactivated": 1, "probed": 7},
         "checked_at": "2026-10-15T12:00:00+00:00", "scheduler": {"enabled": False, "phase": "disabled", "interval_minutes": 30, "progress": {}}}


def route(r):
    if r.request.url.endswith("/api/sub2api-401/state"):
        r.fulfill(status=200, content_type="application/json", body=json.dumps(state))
    else:
        r.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)


rows = lambda page: page.locator("#rows tr input[type=checkbox]").count()
out = {}
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    page.route("http://probe.test/**", route)
    page.goto("http://probe.test/sub2api-401")
    page.wait_for_selector("#rows tr input[type=checkbox]")
    has_filter = page.locator("#stateFilter").count() == 1
    out["filter_present"] = has_filter
    out["rows_all"] = rows(page)
    out["deactivated_row_selectable"] = page.locator('#rows input[data-account-id="3"]').is_enabled()
    if has_filter:
        out["filter_options"] = page.locator("#stateFilter option").all_inner_texts()
        page.select_option("#stateFilter", "deleted")
        out["rows_deleted"] = rows(page)
        page.select_option("#stateFilter", "deactivated")
        page.check("#selectAll")
        out["deactivated_buttons"] = [page.locator("#retryBtn").inner_text(), page.locator("#cleanupBtn").inner_text()]
        page.select_option("#stateFilter", "todo")
    page.check("#selectAll")
    out["after_select_all_buttons"] = [page.locator("#retryBtn").inner_text(), page.locator("#cleanupBtn").inner_text()]
    browser.close()
print(json.dumps(out, ensure_ascii=False, sort_keys=True))
