import unittest
from pathlib import Path


class Cpa401TemplateTests(unittest.TestCase):
    def test_modern_and_legacy_navigation_link_to_same_page(self):
        modern = Path("webui/templates/index.html").read_text(encoding="utf-8")
        legacy = Path("webui/templates/index_legacy.html").read_text(encoding="utf-8")
        self.assertIn('href="/cpa-401"', modern)
        self.assertIn("location.href='/cpa-401'", legacy)

    def test_audit_file_is_persisted_by_container_data_mount(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"cpa_401_audit.json"', dockerfile)
        self.assertIn('ln -s "/data/$path" "/app/$path"', dockerfile)

    def test_page_has_snapshot_retry_verify_and_review_only_cleanup_controls(self):
        page = Path("webui/templates/cpa_401.html").read_text(encoding="utf-8")
        for text in (
            "检测 CPA 401",
            "补跑 Codex",
            "生成清理审阅",
            "补跑前文件",
            "补跑后文件",
            "唯一替代",
            "待清理旧文件",
            "/api/cpa-401/state",
            "/api/cpa-401/detect",
            "/api/cpa-401/retry",
            "/api/cpa-401/cleanup",
            "cleanup_supported",
            "confirm_cleanup",
        ):
            self.assertIn(text, page)
        self.assertIn("绝不猜测删除接口", page)
        self.assertNotIn("/v0/management/auth-files/delete", page)
        self.assertNotIn("/api/codex/delete", page)


if __name__ == "__main__":
    unittest.main()
