import json
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import patch

from core import account_inspection_service, db
from webui.app import _compact_account_for_list, _compact_job_for_list, create_app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    ROOT / "webui" / "templates" / "index.html",
    ROOT / "webui" / "templates" / "index_legacy.html",
)


class AccountTrafficBackendTests(unittest.TestCase):
    def _client(self):
        # Keep app construction isolated from recovery/import side effects.
        with patch.object(
            account_inspection_service,
            "startup_recovery_guard",
            return_value=nullcontext(False),
        ), patch("core.chatgpt2api_import_service.start_import_worker"):
            return create_app(auth_code="test-auth").test_client()

    def test_compact_account_omits_persisted_traffic_and_secret_shaped_values(self):
        traffic = {
            "available": True,
            "total_bytes": "4096",
            "upload_bytes": 1024,
            "download_bytes": 3072,
            "request_count": 8,
            "request_elapsed_seconds": 1.25,
            "http_error_count": 1,
            "transport_error_count": 2,
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

        self.assertNotIn("network_traffic", compact)
        self.assertNotIn("traffic-canary", json.dumps(compact, sort_keys=True))

    def test_accounts_api_omits_traffic_projection_from_compact_response(self):
        traffic = {
            "available": True,
            "total_bytes": 4096,
            "upload_bytes": 1024,
            "download_bytes": 3072,
            "request_count": 8,
            "request_elapsed_seconds": 1.25,
            "http_error_count": 1,
            "transport_error_count": 2,
            "token": "traffic-canary-token",
            "details": [{"body": "traffic-canary-body"}],
        }
        row = {
            "id": 885,
            "email": "account@example.test",
            "extra_json": json.dumps({"network_traffic": traffic}),
        }
        client = self._client()
        with patch.object(
            db,
            "list_accounts_page",
            return_value={"items": [row], "total": 1},
        ):
            response = client.get(
                "/api/accounts?paged=1&page=1&page_size=20",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn("network_traffic", payload["items"][0])
        self.assertNotIn("traffic-canary", response.get_data(as_text=True))

    def test_accounts_api_omits_traffic_projection_from_non_paged_response(self):
        row = {
            "id": 886,
            "email": "legacy@example.test",
            "extra_json": json.dumps({
                "network_traffic": {
                    "available": True,
                    "total_bytes": 4096,
                    "details": [{"body": "traffic-canary-body"}],
                },
                "secret": "traffic-canary-secret",
            }),
        }
        client = self._client()
        with patch.object(db, "list_accounts", return_value=[row]):
            response = client.get(
                "/api/accounts?limit=20",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertNotIn("network_traffic", payload[0])
        self.assertNotIn("extra_json", payload[0])
        self.assertNotIn("traffic-canary", response.get_data(as_text=True))

    def test_malformed_account_metadata_is_ignored_without_text_passthrough(self):
        rows = (
            {"extra_json": "not-json"},
            {"extra_json": "[]"},
            {"extra_json": {"network_traffic": None}},
            {"extra_json": {"network_traffic": {"available": "true"}}},
            {
                "extra_json": {
                    "network_traffic": {
                        "available": True,
                        "total_bytes": "<script>traffic-canary</script>",
                    },
                    "secret": "traffic-canary-secret",
                }
            },
        )
        for row in rows:
            with self.subTest(row=row):
                compact = _compact_account_for_list(row)
                self.assertNotIn("network_traffic", compact)
                self.assertNotIn("traffic-canary", json.dumps(compact))

    def test_registration_job_compact_api_keeps_safe_traffic_summary(self):
        traffic = {
            "available": True,
            "method": "curl_cffi.protocol",
            "label": "Protocol",
            "request_count": 2,
            "upload_bytes": 10,
            "download_bytes": 20,
            "total_bytes": 30,
        }
        row = {
            "id": 7,
            "status": "success",
            "email": "user@example.test",
            "network_traffic": traffic,
        }
        client = self._client()
        with patch.object(
            db,
            "list_jobs_page",
            return_value={"items": [row], "total": 1},
        ), patch.object(db, "job_status_counts", return_value={"success": 1}), patch(
            "webui.app.svc.get_retry_info", return_value={}
        ):
            response = client.get(
                "/api/jobs?paged=1&page=1&page_size=20",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["items"][0]["network_traffic"], traffic)
        self.assertEqual(_compact_job_for_list(row)["network_traffic"], traffic)


class AccountTrafficTemplateTests(unittest.TestCase):
    def test_account_tables_restore_prior_twelve_column_layout(self):
        expected_colgroup = (
            '<col class="col-token"><col class="col-password"><col class="col-plan">'
            '<col class="col-note"><col class="col-small">'
        )
        for path in TEMPLATES:
            page = path.read_text(encoding="utf-8")
            table_class = "accounts-table-v2" if path.name == "index.html" else "accounts-table"
            table_start = page.index(f'<table class="{table_class}">')
            table = page[table_start:page.index("</table>", table_start)]
            with self.subTest(template=path.name):
                self.assertIn(expected_colgroup, table)
                self.assertEqual(table.count("<col "), 12)
                self.assertNotIn("流量", table)
                self.assertNotIn("network_traffic", table)
                self.assertNotIn("col-traffic", table)
                self.assertNotIn("formatAccountTraffic", page)
                self.assertIn('colspan="12"', page)

    def test_registration_job_traffic_rendering_remains_in_both_templates(self):
        for path in TEMPLATES:
            page = path.read_text(encoding="utf-8")
            with self.subTest(template=path.name):
                self.assertIn("formatTraffic(j.network_traffic)", page)
                self.assertIn("col-traffic", page)
                self.assertIn("<th", page)


if __name__ == "__main__":
    unittest.main()
