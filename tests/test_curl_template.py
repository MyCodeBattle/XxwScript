from __future__ import annotations

import json
import unittest

from aftersale_exporter.curl_template import (
    ExportFilterConfig,
    SessionSeed,
    load_filter_config,
    parse_seed_curl,
)


SEED_CURL = r"""
curl 'https://fxg.jinritemai.com/ffa/maftersale/aftersale/list?appid=1&__token=abc&_bid=ffa_aftersale&aid=4272&aftersale_platform_source=fxg&msToken=seed-token&a_bogus=seed-bogus&verifyFp=verify-seed&fp=verify-seed&_lid=oldlid&noise=drop-me' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'content-type: application/json;charset=UTF-8' \
  -H 'cookie: sessionid=abc123; sid_guard=guard456' \
  -H 'x-secsdk-csrf-token: csrf-token' \
  -b 'ttwid=ttwid-value'
"""

FILTER_JSON = """
{
  "order_by": ["status_deadline asc"],
  "conf_version": "v13",
  "after_sale_status": "audit_refunded",
  "order_flag": []
}
"""


class SeedCurlTests(unittest.TestCase):
    def test_parse_seed_curl_extracts_whitelisted_query_and_normalized_cookies(self) -> None:
        seed = parse_seed_curl(SEED_CURL)

        self.assertEqual(seed.origin, "https://fxg.jinritemai.com")
        self.assertEqual(
            seed.query,
            {
                "appid": "1",
                "__token": "abc",
                "_bid": "ffa_aftersale",
                "aid": "4272",
                "aftersale_platform_source": "fxg",
                "msToken": "seed-token",
                "a_bogus": "seed-bogus",
                "verifyFp": "verify-seed",
                "fp": "verify-seed",
            },
        )
        self.assertEqual(seed.headers["accept"], "application/json, text/plain, */*")
        self.assertEqual(seed.headers["content-type"], "application/json;charset=UTF-8")
        self.assertNotIn("cookie", seed.headers)
        self.assertEqual(
            seed.cookies,
            {
                "sessionid": "abc123",
                "sid_guard": "guard456",
                "ttwid": "ttwid-value",
            },
        )

    def test_parse_seed_curl_rejects_non_fxg_domain(self) -> None:
        with self.assertRaisesRegex(ValueError, "fxg.jinritemai.com"):
            parse_seed_curl(
                "curl 'https://example.com/path?appid=1&__token=abc&_bid=b&aid=1&aftersale_platform_source=fxg&msToken=x&a_bogus=y&verifyFp=z&fp=z'"
            )

    def test_parse_seed_curl_reports_missing_required_query_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "msToken"):
            parse_seed_curl(
                "curl 'https://fxg.jinritemai.com/path?appid=1&__token=abc&_bid=b&aid=1&aftersale_platform_source=fxg&a_bogus=y&verifyFp=z&fp=z'"
            )

    def test_build_requests_use_fixed_endpoint_paths_and_runtime_lid(self) -> None:
        seed = parse_seed_curl(SEED_CURL)
        filter_config = load_filter_config(FILTER_JSON)

        export_request = seed.build_export_request(
            filter_config=filter_config,
            start_ts=111,
            end_ts=222,
            request_lid="newlid",
        )
        tasks_request = seed.build_tasks_request(
            task_ids=["task-a", "task-b"],
            request_lid="tasks-lid",
        )
        download_request = seed.build_download_request(
            task_id="task-a",
            request_lid="download-lid",
        )

        self.assertEqual(
            export_request.url,
            "https://fxg.jinritemai.com/shopuser/aftersale/export",
        )
        self.assertEqual(
            tasks_request.url,
            "https://fxg.jinritemai.com/shopuser/aftersale/export/tasks",
        )
        self.assertEqual(
            download_request.url,
            "https://fxg.jinritemai.com/shopuser/aftersale/export/download",
        )
        self.assertEqual(export_request.params["_lid"], "newlid")
        self.assertEqual(tasks_request.params["task_id_list"], "task-a,task-b")
        self.assertEqual(tasks_request.params["_lid"], "tasks-lid")
        self.assertEqual(download_request.params["task_id"], "task-a")
        self.assertEqual(download_request.params["_lid"], "download-lid")
        self.assertEqual(export_request.json["apply_time_start"], 111)
        self.assertEqual(export_request.json["apply_time_end"], 222)
        self.assertEqual(export_request.json["_lid"], "newlid")
        self.assertEqual(export_request.json["after_sale_status"], "audit_refunded")

    def test_filter_config_requires_non_empty_object(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty JSON object"):
            load_filter_config("[]")

        with self.assertRaisesRegex(ValueError, "non-empty JSON object"):
            load_filter_config("{}")

    def test_session_seed_round_trip_preserves_json_serializability(self) -> None:
        encoded = json.dumps(SessionSeed.model_dump(parse_seed_curl(SEED_CURL)), sort_keys=True)

        self.assertIn('"appid": "1"', encoded)
        self.assertIn('"sessionid": "abc123"', encoded)

    def test_filter_config_round_trip_preserves_body(self) -> None:
        encoded = json.dumps(ExportFilterConfig.model_dump(load_filter_config(FILTER_JSON)), sort_keys=True)

        self.assertIn('"after_sale_status": "audit_refunded"', encoded)


if __name__ == "__main__":
    unittest.main()
