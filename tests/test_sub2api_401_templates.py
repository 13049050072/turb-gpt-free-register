import unittest
from pathlib import Path


class Sub2Api401TemplateTests(unittest.TestCase):
    def test_modern_and_legacy_navigation_link_to_sub2api_page_only(self):
        modern = Path("webui/templates/index.html").read_text(encoding="utf-8")
        legacy = Path("webui/templates/index_legacy.html").read_text(encoding="utf-8")
        self.assertIn('href="/sub2api-401"', modern)
        self.assertIn("location.href='/sub2api-401'", legacy)
        self.assertNotIn("cpa_401_page", modern)
        self.assertNotIn("location.href='/cpa-401'", legacy)

    def test_audit_file_is_persisted_by_container_data_mount(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"sub2api_401_audit.json"', dockerfile)
        self.assertNotIn('"cpa_401_audit.json"', dockerfile)
        self.assertIn('ln -s "/data/$path" "/app/$path"', dockerfile)

    def test_page_has_account_id_snapshot_retry_verify_and_explicit_cleanup_controls(self):
        page = Path("webui/templates/sub2api_401.html").read_text(encoding="utf-8")
        for text in (
            "检测 Sub2API 401",
            "补跑 Codex",
            "重试清理旧 ID",
            "按钮仅用于历史重复项或自动清理失败重试",
            "补跑前 account_id",
            "补跑后 account_id",
            "唯一替代 account_id",
            "锁定清理 ID",
            "/api/sub2api-401/state",
            "/api/sub2api-401/detect",
            "/api/sub2api-401/retry",
            "/api/sub2api-401/cleanup",
            "account_ids",
            "cleanup_supported",
            "confirm_cleanup",
            "不会按邮箱批量删除",
        ):
            self.assertIn(text, page)
        self.assertNotIn("/api/cpa-401", page)
        self.assertNotIn("auth-files", page)
        self.assertIn('<option selected>3</option>', page)
        self.assertIn("workers:Number($('#workers').value || 3)", page)

    def test_page_polls_retrying_state_until_terminal(self):
        page = Path("webui/templates/sub2api_401.html").read_text(encoding="utf-8")
        for text in (
            "let pollTimer = null;",
            "let stateLoading = false;",
            "const retrying = items.some(item => item.state === 'retrying');",
            "const schedulerRunning = scheduler.phase === 'running' || scheduler.phase === 'waiting_for_queue';",
            "const schedulerDue = scheduler.enabled === true && Number.isFinite(nextRunAt) && nextRunAt <= Date.now() + 3000;",
            "const delay = retrying || schedulerRunning || schedulerDue ? 3000 : (scheduler.enabled ? 30000 : 0);",
            "if (pollTimer && pollDelay !== delay) {",
            "clearTimeout(pollTimer);",
            "pollDelay = 0;",
            "if (!delay) return;",
            "pollTimer = setTimeout(async () => {",
            "await loadState();",
            "}, delay);",
            "String(error?.message || '状态读取失败').slice(0, 160)",
            "schedulePolling();",
            "if (stateLoading) return;",
            "function applyData(data) { items = data.items || []; cleanupSupported = data.cleanup_supported !== false; applyScheduler(data.scheduler || scheduler); updateCounts(data); render(); schedulePolling(); }",
            "  loadState();\n})();",
        ):
            self.assertIn(text, page)
        self.assertEqual(page.count("/api/sub2api-401/detect"), 1)

    def test_page_has_independent_scheduler_controls(self):
        page = Path("webui/templates/sub2api_401.html").read_text(encoding="utf-8")
        for text in (
            "开启 Sub2API 自动巡检",
            "30 分钟",
            "1 小时",
            'min="5" max="1440"',
            "/api/sub2api-401/scheduler",
            "enabled:$('#autoEnabled').checked",
            "interval_minutes:interval",
            "关闭页面不会停止巡检",
            "精确旧 ID 清理流程",
        ):
            self.assertIn(text, page)


if __name__ == "__main__":
    unittest.main()
