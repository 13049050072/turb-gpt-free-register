# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class ChatGPT2API401TemplateTests(unittest.TestCase):
    def test_page_has_separate_safe_replacement_workflow(self):
        page = Path("webui/templates/chatgpt2api_401.html").read_text(encoding="utf-8")
        for text in (
            "ChatGPT2API 401 AT 安全替换",
            "检测失效 / 401",
            "查活刷新",
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
        ):
            self.assertIn(text, page)
        self.assertNotIn("/api/sub2api-401/", page)

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
