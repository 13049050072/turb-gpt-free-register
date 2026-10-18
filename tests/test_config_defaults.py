# -*- coding: utf-8 -*-
import os
import importlib
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import config as config_pkg
from config import env_loader
from config import browser
from config import openai_protocol
from webui import config_editor


@contextmanager
def _browser_source_defaults(*, liveness_impersonate: str = ""):
    """测试源码默认值，不让开发机 .env 的显式 URL 覆盖影响断言。"""
    old_loaded = env_loader._LOADED
    try:
        with patch.dict(os.environ, {
            "BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS": "",
            "LIVENESS_IMPERSONATE": liveness_impersonate,
        }, clear=False):
            env_loader._LOADED = True
            importlib.reload(browser)
            config_pkg._refresh_top_level_constants()
            yield
    finally:
        env_loader._LOADED = old_loaded
        importlib.reload(browser)
        config_pkg._refresh_top_level_constants()


@contextmanager
def _protocol_source_defaults(*, auth_bootstrap_enabled: str = ""):
    old_loaded = env_loader._LOADED
    try:
        with patch.dict(os.environ, {"CHATGPT_AUTH_BOOTSTRAP_ENABLED": auth_bootstrap_enabled}, clear=False):
            env_loader._LOADED = True
            importlib.reload(openai_protocol)
            config_pkg._refresh_top_level_constants()
            yield
    finally:
        env_loader._LOADED = old_loaded
        importlib.reload(openai_protocol)
        config_pkg._refresh_top_level_constants()


class ConfigDefaultFallbackTests(unittest.TestCase):
    def test_blank_env_value_uses_default_for_all_supported_types(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        try:
            with patch.dict(os.environ, {
                "BOOL_KEY": "",
                "INT_KEY": "",
                "FLOAT_KEY": "",
                "STR_KEY": "",
                "LIST_KEY": "",
            }, clear=True):
                self.assertTrue(env_loader.env_bool("BOOL_KEY", True))
                self.assertEqual(env_loader.env_int("INT_KEY", 90), 90)
                self.assertEqual(env_loader.env_float("FLOAT_KEY", 1.5), 1.5)
                self.assertEqual(env_loader.env_str("STR_KEY", "default"), "default")
                self.assertEqual(env_loader.env_list("LIST_KEY", ["a"]), ["a"])
        finally:
            env_loader._LOADED = old_loaded

    def test_proxy_pool_blank_env_value_means_empty_list(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"PROXY_POOL": ["socks5://127.0.0.1:7897"]}
        try:
            with patch.dict(os.environ, {"PROXY_POOL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"PROXY_POOL": "list_str_multiline"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["PROXY_POOL"], [])

    def test_config_editor_formats_empty_list_as_literal_empty_list(self):
        self.assertEqual(config_editor._format_env_value([], "list_str_multiline"), "[]")

    def test_apply_env_overrides_does_not_let_blank_values_mask_defaults(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"FEATURE_ENABLED": True, "BASE_URL": "https://example.test"}
        try:
            with patch.dict(os.environ, {"FEATURE_ENABLED": "", "BASE_URL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"FEATURE_ENABLED": "bool", "BASE_URL": "str"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertTrue(namespace["FEATURE_ENABLED"])
        self.assertEqual(namespace["BASE_URL"], "https://example.test")

    def test_config_editor_parses_env_str_default_from_source(self):
        source = 'API_KEY: str = env_str("API_KEY", "fallback-key")\n'
        self.assertEqual(
            config_editor._parse_value_from_source(source, "API_KEY", "str"),
            "fallback-key",
        )

    def test_config_editor_blank_env_value_falls_back_to_source_default(self):
        self.assertEqual(
            config_editor._coerce_raw_value("", "wss://connect.browser-use.com", "str"),
            "wss://connect.browser-use.com",
        )
        self.assertTrue(config_editor._coerce_raw_value("", True, "bool"))

    def test_chatgpt2api_defaults_to_session_json_mode(self):
        source = Path("config/chatgpt2api.py").read_text(encoding="utf-8")
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertIn('CHATGPT2API_CREDENTIAL_MODE: str = "session_json"', source)
        self.assertEqual(
            fields["CHATGPT2API_CREDENTIAL_MODE"]["choices"][0]["value"],
            "session_json",
        )

        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["BROWSER_DATA_SAVER_MODE"]["type"], "bool")
        self.assertEqual(fields["BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES"]["type"], "list_str_multiline")
        self.assertEqual(fields["BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS"]["type"], "list_str_multiline")
        self.assertEqual(fields["BROWSER_TRAFFIC_DETAIL_LOG"]["type"], "bool")
        self.assertEqual(fields["BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES"]["type"], "int")
        self.assertEqual(fields["BROWSER_JS_COVERAGE_LOG"]["type"], "bool")
        self.assertEqual(fields["BROWSER_JS_COVERAGE_MAX_ENTRIES"]["type"], "int")

    def test_protocol_traffic_uses_existing_detail_config_defaults_and_editor_copy(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        detail_log = fields["BROWSER_TRAFFIC_DETAIL_LOG"]
        detail_limit = fields["BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES"]
        source = Path("config/browser.py").read_text(encoding="utf-8")
        env_example = Path(".env.example").read_text(encoding="utf-8")

        self.assertIn("浏览器/协议", detail_log["label"])
        self.assertIn("protocol", detail_log["help"])
        self.assertIn("scheme/host/path", detail_log["help"])
        self.assertIn("protocol", detail_limit["help"])
        self.assertIn("BROWSER_TRAFFIC_DETAIL_LOG: bool = False", source)
        self.assertIn("BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES: int = 2000", source)
        self.assertIn('BROWSER_TRAFFIC_DETAIL_LOG="False"', env_example)
        self.assertIn('BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES="2000"', env_example)

    def test_protocol_authenticated_bootstrap_defaults_off_and_supports_opt_in(self):
        with _protocol_source_defaults():
            self.assertFalse(openai_protocol.CHATGPT_AUTH_BOOTSTRAP_ENABLED)
            self.assertFalse(config_pkg.CHATGPT_AUTH_BOOTSTRAP_ENABLED)
            self.assertTrue(openai_protocol.CHATGPT_ANON_BOOTSTRAP_ENABLED)

        with _protocol_source_defaults(auth_bootstrap_enabled="True"):
            self.assertTrue(openai_protocol.CHATGPT_AUTH_BOOTSTRAP_ENABLED)
            self.assertTrue(config_pkg.CHATGPT_AUTH_BOOTSTRAP_ENABLED)

        env_example = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn('CHATGPT_AUTH_BOOTSTRAP_ENABLED="False"', env_example)

    def test_browser_data_saver_defaults_block_google_gsi(self):
        with _browser_source_defaults():
            self.assertIn(
                "**://accounts.google.com/gsi/client**",
                browser.BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS,
            )

    def test_browser_data_saver_defaults_block_chatgpt_rum(self):
        with _browser_source_defaults():
            self.assertIn(
                "**://chatgpt.com/awe/api/v2/rum**",
                browser.BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS,
            )

    def test_browser_data_saver_defaults_do_not_block_chatgpt_core_bundles(self):
        with _browser_source_defaults():
            for prefix in (
                "conversation-small",
                "7aaae702",
                "8b34dbc2",
                "c2675c8c",
                "4813494d",
                "12e5f202",
                "2340486e",
                "25a66342",
            ):
                self.assertNotIn(
                    f"**://chatgpt.com/cdn/assets/{prefix}-*.js**",
                    browser.BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS,
                )

    def test_browser_transport_uses_current_chrome146_fingerprint(self):
        from curl_cffi import requests

        with _browser_source_defaults():
            self.assertEqual(browser.CHROME_MAJOR, "146")
            self.assertEqual(browser.IMPERSONATE, "chrome146")
            self.assertEqual(browser.LIVENESS_IMPERSONATE, "chrome124")
            self.assertIn(f"Chrome/{browser.CHROME_FULL_VERSION}", browser.USER_AGENT)
            self.assertIn('"Google Chrome";v="146"', browser.SEC_CH_UA)
            session = requests.Session(impersonate=browser.IMPERSONATE)
            session.close()

    def test_liveness_transport_env_accepts_only_fully_verified_profile(self):
        with _browser_source_defaults(liveness_impersonate=" chrome124 "):
            self.assertEqual(browser.LIVENESS_IMPERSONATE, "chrome124")
            self.assertEqual(
                browser.LIVENESS_IMPERSONATE_ALLOWED,
                frozenset({"chrome124"}),
            )

    def test_unverified_or_invalid_liveness_transport_env_falls_back_to_chrome124(self):
        for value in ("chrome131", "chrome146", "arbitrary-client"):
            with self.subTest(value=value), _browser_source_defaults(liveness_impersonate=value):
                self.assertEqual(browser.LIVENESS_IMPERSONATE, "chrome124")
                self.assertEqual(browser.IMPERSONATE, "chrome146")

    def test_reload_all_keeps_registration_on_global_default(self):
        old_loaded = env_loader._LOADED
        try:
            with patch.dict(
                os.environ,
                {
                    "LIVENESS_IMPERSONATE": "chrome131",
                    "REGISTRATION_IMPERSONATE": "chrome146",
                },
                clear=False,
            ), patch.object(config_pkg, "_RELOADABLE_SUBMODULES", ("config.browser",)), patch.object(
                env_loader, "load_env", return_value=env_loader.env_path()
            ):
                env_loader._LOADED = True
                self.assertEqual(config_pkg.reload_all(), ["config.browser"])
                self.assertEqual(browser.LIVENESS_IMPERSONATE, "chrome124")
                self.assertEqual(config_pkg.LIVENESS_IMPERSONATE, "chrome124")
                self.assertFalse(hasattr(browser, "REGISTRATION_IMPERSONATE"))
                self.assertFalse(hasattr(config_pkg, "REGISTRATION_IMPERSONATE"))
        finally:
            env_loader._LOADED = old_loaded
            importlib.reload(browser)
            config_pkg._refresh_top_level_constants()

    def test_reload_all_refreshes_validated_liveness_package_exports(self):
        old_loaded = env_loader._LOADED
        try:
            with patch.dict(os.environ, {"LIVENESS_IMPERSONATE": "arbitrary-client"}, clear=False), \
                 patch.object(config_pkg, "_RELOADABLE_SUBMODULES", ("config.browser",)), \
                 patch.object(env_loader, "load_env", return_value=env_loader.env_path()):
                env_loader._LOADED = True
                self.assertEqual(config_pkg.reload_all(), ["config.browser"])
                self.assertEqual(browser.LIVENESS_IMPERSONATE, "chrome124")
                self.assertEqual(config_pkg.LIVENESS_IMPERSONATE, "chrome124")
                self.assertFalse(hasattr(config_pkg, "REGISTRATION_IMPERSONATE"))
                self.assertEqual(config_pkg.LIVENESS_IMPERSONATE_ALLOWED, frozenset({"chrome124"}))
                self.assertEqual(config_pkg.IMPERSONATE, "chrome146")
        finally:
            env_loader._LOADED = old_loaded
            importlib.reload(browser)
            config_pkg._refresh_top_level_constants()


if __name__ == "__main__":
    unittest.main()
