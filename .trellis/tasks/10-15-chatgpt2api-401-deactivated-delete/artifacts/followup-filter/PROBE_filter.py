# -*- coding: utf-8 -*-
"""Same-input browser probe for the ChatGPT2API 401 page state filter.

Usage: python -I PROBE_filter.py <repo_root>
Serves <repo_root>/webui/templates/chatgpt2api_401.html with a mocked state API
(3 deleted, 2 pending, 1 failed, 1 conflict, 1 replaced) in headless Chromium.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

page_html = (Path(sys.argv[1]) / "webui/templates/chatgpt2api_401.html").read_text(encoding="utf-8")


def item(n, state, retryable=False):
    return {
        "remote_id": f"acct_{n}", "email": f"u{n}@example.com", "local_account_id": n,
        "state": state, "reason": state, "failure_stage": "", "old_at_fingerprint": "aaaa",
        "new_at_fingerprint": "", "detected_at": "2026-10-15T11:00:00+00:00", "queued_at": "",
        "refresh_started_at": "", "refresh_completed_at": "", "replaced_at": "", "deleted_at": "",
        "retryable": retryable, "replaceable": False,
    }


items = [item(1, "deleted"), item(2, "deleted"), item(3, "deleted"),
         item(4, "pending", True), item(5, "pending", True), item(6, "failed", True),
         item(7, "conflict"), item(8, "replaced")]
counts = {s: sum(1 for i in items if i["state"] == s) for s in
          ("pending", "queued", "refreshing", "ready", "replaced", "deleted", "failed", "ambiguous", "conflict")}
counts.update({"total": 70, "invalid": 0, "healthy": 70})
state = {"ok": True, "items": items, "counts": counts, "checked_at": "2026-10-15T11:26:25+00:00",
         "progress": {"workers": 5, "queued": 0, "active": 0, "completed": 4, "failed": 1},
         "scheduler": {"enabled": False, "interval_minutes": 30, "phase": "disabled", "progress": {}}}


def route(r):
    if r.request.url.endswith("/api/chatgpt2api-401/state"):
        r.fulfill(status=200, content_type="application/json", body=json.dumps(state))
    else:
        r.fulfill(status=200, content_type="text/html; charset=utf-8", body=page_html)


def rows(page):
    return page.locator("#rows tr input[type=checkbox]").count()


result = {}
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    page.route("http://probe.test/**", route)
    page.goto("http://probe.test/chatgpt2api-401")
    page.wait_for_selector("#rows tr input[type=checkbox]")
    has_filter = page.locator("#stateFilter").count() == 1
    result["filter_present"] = has_filter
    result["rows_all"] = rows(page)
    if has_filter:
        result["filter_options"] = page.locator("#stateFilter option").all_inner_texts()
        page.select_option("#stateFilter", "deleted")
        result["rows_deleted"] = rows(page)
        result["badges_deleted"] = sorted(set(page.locator("#rows .badge").all_inner_texts()))
        result["summary_deleted"] = page.locator("#filterSummary").inner_text()
        page.select_option("#stateFilter", "todo")
        result["rows_todo"] = rows(page)
    page.check("#selectAll")
    result["refresh_btn_after_select_all"] = page.locator("#refreshBtn").inner_text()
    if has_filter:
        page.select_option("#stateFilter", "deleted")
        result["refresh_btn_after_switch_to_deleted"] = page.locator("#refreshBtn").inner_text()
        page.reload()
        page.wait_for_selector("#rows tr input[type=checkbox]")
        result["filter_after_reload"] = page.locator("#stateFilter").input_value()
        result["rows_after_reload"] = rows(page)
    browser.close()
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
