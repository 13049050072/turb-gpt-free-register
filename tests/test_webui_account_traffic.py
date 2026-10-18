import json
from pathlib import Path
import shutil
import subprocess
import unittest

from webui.app import _compact_account_for_list


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    ROOT / "webui" / "templates" / "index.html",
    ROOT / "webui" / "templates" / "index_legacy.html",
)


class AccountTrafficBackendTests(unittest.TestCase):
    def test_compact_account_exposes_only_sanitized_aggregate_allowlist(self):
        traffic = {
            "available": True,
            "total_bytes": "4096",
            "upload_bytes": 1024.0,
            "download_bytes": 3072,
            "request_count": "8",
            "request_elapsed_seconds": "1.25",
            "http_error_count": 1,
            "transport_error_count": "2",
            "unknown_size_request_count": 3,
            "incomplete_size_request_count": "4",
            "url": "https://traffic-canary.invalid/private?token=traffic-canary",
            "details": [{"body": "traffic-canary-body"}],
            "headers": {"Authorization": "traffic-canary-credential"},
            "exception": "traffic-canary-exception",
            "access_token": "traffic-canary-token",
        }
        compact = _compact_account_for_list({
            "id": 885,
            "email": "account@example.test",
            "extra_json": json.dumps({"network_traffic": traffic}),
        })

        self.assertEqual(compact["network_traffic"], {
            "available": True,
            "total_bytes": 4096,
            "upload_bytes": 1024,
            "download_bytes": 3072,
            "request_count": 8,
            "http_error_count": 1,
            "transport_error_count": 2,
            "request_elapsed_seconds": 1.25,
            "unknown_size_request_count": 3,
            "incomplete_size_request_count": 4,
        })
        serialized = json.dumps(compact, sort_keys=True)
        self.assertNotIn("traffic-canary", serialized)
        self.assertNotIn("details", serialized)
        self.assertNotIn("headers", serialized)
        self.assertNotIn("exception", serialized)

    def test_malformed_or_secret_shaped_metrics_are_omitted_without_text_passthrough(self):
        traffic = {
            "available": True,
            "total_bytes": 100,
            "upload_bytes": 40,
            "download_bytes": 60,
            "request_count": 2,
            "request_elapsed_seconds": 0.5,
            "http_error_count": 0,
            "transport_error_count": 0,
            "unknown_size_request_count": '<img src=x onerror="traffic-canary">',
            "incomplete_size_request_count": -1,
            "token": "traffic-canary-token",
        }
        compact = _compact_account_for_list({"extra_json": {"network_traffic": traffic}})
        sanitized = compact["network_traffic"]
        self.assertNotIn("unknown_size_request_count", sanitized)
        self.assertNotIn("incomplete_size_request_count", sanitized)
        self.assertNotIn("token", sanitized)
        self.assertNotIn("traffic-canary", json.dumps(compact))

        traffic["total_bytes"] = '<script id="traffic-canary">'
        self.assertNotIn(
            "network_traffic",
            _compact_account_for_list({"extra_json": {"network_traffic": traffic}}),
        )

    def test_absent_unavailable_and_malformed_payload_traffic_is_omitted(self):
        rows = (
            {},
            {"extra_json": "not-json"},
            {"extra_json": "[]"},
            {"extra_json": {"network_traffic": None}},
            {"extra_json": {"network_traffic": {"available": False}}},
            {"extra_json": {"network_traffic": {"available": "true"}}},
        )
        for row in rows:
            with self.subTest(row=row):
                self.assertNotIn("network_traffic", _compact_account_for_list(row))

    def test_integer_metrics_preserve_exact_js_safe_range(self):
        traffic = {
            "available": True,
            "total_bytes": "9007199254740991",
            "upload_bytes": 0,
            "download_bytes": 0,
            "request_count": 0,
            "request_elapsed_seconds": 0,
            "http_error_count": 0,
            "transport_error_count": 0,
        }
        compact = _compact_account_for_list({"extra_json": {"network_traffic": traffic}})
        self.assertEqual(compact["network_traffic"]["total_bytes"], 9_007_199_254_740_991)

        for malformed in ("9007199254740990.5", "9007199254740991.1", "9007199254740992"):
            with self.subTest(value=malformed):
                traffic["total_bytes"] = malformed
                self.assertNotIn(
                    "network_traffic",
                    _compact_account_for_list({"extra_json": {"network_traffic": traffic}}),
                )


class AccountTrafficTemplateTests(unittest.TestCase):
    def test_both_account_tables_have_traffic_column_and_safe_renderer(self):
        for path in TEMPLATES:
            page = path.read_text(encoding="utf-8")
            table_class = "accounts-table-v2" if path.name == "index.html" else "accounts-table"
            table_start = page.index(f'<table class="{table_class}">')
            table = page[table_start:page.index("</table>", table_start)]
            with self.subTest(template=path.name):
                self.assertLess(table.index("套餐"), table.index("流量"))
                self.assertLess(table.index("流量"), table.index("备注"))
                self.assertIn('<col class="col-plan"><col class="col-traffic"><col class="col-note">', table)
                self.assertIn("${formatAccountTraffic(r.network_traffic)}", page)
                self.assertIn('colspan="13"', page)
                self.assertIn("Number.isFinite(n) && n >= 0", page)
                self.assertIn("esc(hints.join('；'))", page)

    @unittest.skipUnless(shutil.which("node"), "node is required for template helper execution")
    def test_both_renderers_format_numeric_values_and_drop_injected_text(self):
        for path in TEMPLATES:
            page = path.read_text(encoding="utf-8")
            start = page.index("function trafficNumber(v)")
            end_marker = "function jobsV2OpIcons()" if path.name == "index.html" else "function copyId(v)"
            helpers = page[start:page.index(end_marker, start)]
            script = """
function fmt(v) { return v == null || v === '' ? '-' : String(v); }
function esc(v) { return fmt(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;'); }
""" + helpers + """
const good = formatAccountTraffic({
  available: true,
  total_bytes: 2048,
  upload_bytes: 512,
  download_bytes: 1536,
  request_count: 4,
  request_elapsed_seconds: 1.25,
  http_error_count: 1,
  transport_error_count: 0,
  unknown_size_request_count: 2,
  incomplete_size_request_count: 1,
});
const malformed = formatAccountTraffic({
  available: true,
  total_bytes: '<img src=x onerror=traffic-canary>',
  upload_bytes: 'traffic-canary-upload',
  download_bytes: -1,
  request_count: 'traffic-canary-request',
  request_elapsed_seconds: Infinity,
  http_error_count: {},
  transport_error_count: null,
});
const jobTraffic = formatTraffic({
  available: true,
  total_bytes: 2048,
  upload_bytes: 512,
  download_bytes: 1536,
  request_count: 4,
  js_coverage: {enabled: true, script_count: 2, executed_script_count: 1, candidate_script_count: 3},
});
console.log(JSON.stringify({good, malformed, jobTraffic, unavailable: formatAccountTraffic(null)}));
"""
            result = subprocess.run(
                [shutil.which("node"), "-e", script],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            rendered = json.loads(result.stdout)
            with self.subTest(template=path.name):
                self.assertIn(">2.0 KiB<", rendered["good"])
                self.assertIn("↑ 512 B / ↓ 1.5 KiB", rendered["good"])
                self.assertIn("请求 4；耗时 1.250 秒；HTTP 失败 1；传输失败 0；未知 2；不完整 1", rendered["good"])
                self.assertNotIn("traffic-canary", rendered["malformed"])
                self.assertEqual(rendered["jobTraffic"], '<span title="上传 512 B，下载 1.5 KiB，4 个请求，JS脚本 2（执行 1，候选 3）">2.0 KiB</span>')
                self.assertEqual(rendered["unavailable"], "-")


if __name__ == "__main__":
    unittest.main()
