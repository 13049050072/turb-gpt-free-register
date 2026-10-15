# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class ChatGPT2API401TemplateTests(unittest.TestCase):
    def test_page_has_separate_safe_replacement_workflow(self):
        page = Path("webui/templates/chatgpt2api_401.html").read_text(encoding="utf-8")
        for text in (
            "ChatGPT2API 401 AT 安全替换",
            "检测失效 / 401",
            "查活并写回",
            "后台队列（固定并发 5）",
            "确认原位替换",
            "远端稳定 ID",
            "旧 AT 摘要",
            "新 AT 摘要",
            "/api/chatgpt2api-401/state",
            "/api/chatgpt2api-401/detect",
            "/api/chatgpt2api-401/refresh",
            "/api/chatgpt2api-401/replace",
            "confirm_replace:false",
            "confirm_replace:true",
            "setTimeout",
            "3000",
            "schedulePolling(result)",
            "queued",
            "refreshing",
        ):
            self.assertIn(text, page)
        self.assertNotIn("/api/sub2api-401/", page)
        self.assertNotIn("slice(0, 50)", page)
        self.assertNotIn("完成前请保持页面打开", page)

    def test_page_filters_rows_by_state_and_scopes_selection(self):
        page = Path("webui/templates/chatgpt2api_401.html").read_text(encoding="utf-8")
        for text in (
            '<select id="stateFilter">',
            '<option value="deleted">已删除废号</option>',
            "deleted:['deleted']",
            "todo:['pending','failed']",
            "problem:['conflict','ambiguous']",
            "const visible = items.filter(item => inFilter(item));",
            "if (actionable(item) && inFilter(item)) selected.add(item.remote_id);",
            "const available = visible.filter(actionable);",
            "当前筛选下没有记录。",
            "localStorage.setItem(filterStorageKey, stateFilter)",
        ):
            self.assertIn(text, page)

    def test_page_has_independent_scheduler_controls(self):
        page = Path("webui/templates/chatgpt2api_401.html").read_text(encoding="utf-8")
        for text in (
            "开启 ChatGPT2API 自动巡检",
            "30 分钟",
            "1 小时",
            'min="5" max="1440"',
            "/api/chatgpt2api-401/scheduler",
            "enabled:$('#autoEnabled').checked",
            "interval_minutes:interval",
            "关闭页面不会停止巡检",
            "固定并发 5",
            "精确远端 ID AT 写回",
            "const queueRunning = Number(progress.queued || 0) + Number(progress.active || 0) > 0",
            "items.some(item => item.state === 'queued' || item.state === 'refreshing')",
            "scheduler.phase === 'running' || scheduler.phase === 'waiting_for_queue'",
            "const schedulerDue = scheduler.enabled === true && Number.isFinite(nextRunAt) && nextRunAt <= Date.now() + 3000;",
            "queueRunning || schedulerRunning || schedulerDue ? 3000",
            "scheduler.enabled ? 30000 : 0",
            "pollDelay !== delay",
            "pollDelay = 0",
            "showMessage(error.message, 'error'); schedulePolling();",
        ):
            self.assertIn(text, page)

    def test_runtime_audit_is_ignored_and_docker_persisted(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn('"chatgpt2api_401_audit.json"', dockerfile)
        self.assertIn("chatgpt2api_401_audit.json", gitignore)

    def test_modern_and_legacy_navigation_keep_both_independent_pages(self):
        for path in ("webui/templates/index.html", "webui/templates/index_legacy.html"):
            page = Path(path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn('href="/chatgpt2api-401"' if path.endswith("index.html") else "location.href='/chatgpt2api-401'", page)
                self.assertIn("ChatGPT2API 401", page)
                self.assertIn("/sub2api-401", page)


if __name__ == "__main__":
    unittest.main()
