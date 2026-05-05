from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from aftersale_exporter.api import AftersaleApiService, RequestFailedError, TaskTimeoutError
from aftersale_exporter.curl_template import load_filter_config, parse_seed_curl
from aftersale_exporter.workflow import AuthenticationError, OverLimitError


SEED_CURL = r"""
curl 'https://fxg.jinritemai.com/ffa/maftersale/aftersale/list?appid=1&__token=abc&_bid=ffa_aftersale&aid=4272&aftersale_platform_source=fxg&msToken=seed-token&a_bogus=seed-bogus&verifyFp=verify-seed&fp=verify-seed&_lid=oldlid' \
  -H 'content-type: application/json;charset=UTF-8' \
  -b 'sessionid=abc123'
"""

FILTER_JSON = """
{
  "order_by": ["status_deadline asc"],
  "conf_version": "v13",
  "after_sale_status": "audit_refunded"
}
"""


class FakeResponse:
    def __init__(
        self,
        *,
        json_data=None,
        content: bytes = b"",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._json_data = json_data
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("no fake response left")
        return self.responses.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class AftersaleApiServiceTests(unittest.TestCase):
    def build_service(
        self,
        responses: list[FakeResponse],
        *,
        clock: FakeClock | None = None,
    ) -> tuple[AftersaleApiService, FakeSession]:
        session = FakeSession(responses)
        fake_clock = clock or FakeClock()
        service = AftersaleApiService(
            session_seed=parse_seed_curl(SEED_CURL),
            filter_config=load_filter_config(FILTER_JSON),
            session=session,
            lid_factory=lambda: "generated-lid",
            sleep_fn=fake_clock.sleep,
            monotonic_fn=fake_clock.monotonic,
        )
        return service, session

    def test_create_export_uses_fixed_export_path_and_filter_body(self) -> None:
        service, session = self.build_service(
            [FakeResponse(json_data={"code": 0, "data": {"task_id": "task-123"}})]
        )

        task_id = service.create_export(111, 222)

        self.assertEqual(task_id, "task-123")
        self.assertEqual(
            session.calls[0]["url"],
            "https://fxg.jinritemai.com/shopuser/aftersale/export",
        )
        self.assertEqual(session.calls[0]["params"]["appid"], "1")
        self.assertEqual(session.calls[0]["params"]["_lid"], "generated-lid")
        self.assertEqual(session.calls[0]["json"]["apply_time_start"], 111)
        self.assertEqual(session.calls[0]["json"]["apply_time_end"], 222)
        self.assertEqual(session.calls[0]["json"]["after_sale_status"], "audit_refunded")

    def test_create_export_detects_platform_limit_error(self) -> None:
        service, _ = self.build_service(
            [
                FakeResponse(
                    json_data={
                        "code": 20309001,
                        "msg": "售后单最多支持导出5万条",
                    }
                )
            ]
        )

        with self.assertRaisesRegex(OverLimitError, "5万条"):
            service.create_export(111, 222)

    def test_create_export_does_not_treat_export_cooldown_as_row_limit(self) -> None:
        service, _ = self.build_service(
            [
                FakeResponse(
                    json_data={
                        "code": 20309001,
                        "msg": "店铺3分钟内不允许再次导出，请稍后再试",
                    }
                )
            ]
        )

        with self.assertRaisesRegex(RequestFailedError, "3分钟内不允许再次导出"):
            service.create_export(111, 222)

    def test_create_export_raises_authentication_error_on_http_403(self) -> None:
        service, _ = self.build_service([FakeResponse(status_code=403, json_data={})])

        with self.assertRaises(AuthenticationError):
            service.create_export(111, 222)

    def test_wait_for_task_polls_fixed_tasks_endpoint_until_completed(self) -> None:
        clock = FakeClock()
        service, session = self.build_service(
            [
                FakeResponse(
                    json_data={
                        "code": 0,
                        "data": [{"task_id": "task-1", "status": "running"}],
                    }
                ),
                FakeResponse(
                    json_data={
                        "code": 0,
                        "data": [
                            {
                                "task_id": "task-1",
                                "status": "success",
                                "file_name": "done.csv",
                            }
                        ],
                    }
                ),
            ],
            clock=clock,
        )

        result = service.wait_for_task("task-1", poll_interval=2.0, timeout=5.0)

        self.assertEqual(result.download_name, "done.csv")
        self.assertEqual(
            session.calls[0]["url"],
            "https://fxg.jinritemai.com/shopuser/aftersale/export/tasks",
        )
        self.assertEqual(session.calls[0]["params"]["task_id_list"], "task-1")
        self.assertEqual(session.calls[0]["params"]["_lid"], "generated-lid")
        self.assertEqual(clock.sleeps, [2.0])

    def test_wait_for_task_accepts_task_list_payload_with_numeric_status(self) -> None:
        service, session = self.build_service(
            [
                FakeResponse(
                    json_data={
                        "code": 0,
                        "data": {
                            "task_list": [
                                {
                                    "task_id": "task-1",
                                    "status": 1,
                                    "resource_url": "/shopuser/aftersale/export/download?task_id=task-1",
                                }
                            ]
                        },
                    }
                ),
                FakeResponse(
                    json_data={
                        "code": 0,
                        "data": {
                            "task_list": [
                                {
                                    "task_id": "task-1",
                                    "status": 2,
                                    "resource_url": "/shopuser/aftersale/export/download?task_id=task-1",
                                }
                            ]
                        },
                    }
                ),
            ]
        )

        result = service.wait_for_task("task-1", poll_interval=2.0, timeout=6.0)

        self.assertEqual(result.download_name, "task-1.bin")
        self.assertEqual(len(session.calls), 2)

    def test_wait_for_task_raises_timeout_when_never_completes(self) -> None:
        clock = FakeClock()
        service, _ = self.build_service(
            [
                FakeResponse(
                    json_data={
                        "code": 0,
                        "data": [{"task_id": "task-1", "status": "running"}],
                    }
                ),
                FakeResponse(
                    json_data={
                        "code": 0,
                        "data": [{"task_id": "task-1", "status": "running"}],
                    }
                ),
            ],
            clock=clock,
        )

        with self.assertRaises(TaskTimeoutError):
            service.wait_for_task("task-1", poll_interval=2.0, timeout=3.0)

    def test_download_export_writes_binary_response_to_fixed_download_endpoint(self) -> None:
        service, session = self.build_service(
            [FakeResponse(content=b"csv-bytes", headers={"content-type": "text/csv"})]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "download.csv"
            final_path = service.download_export("task-1", destination)

            self.assertEqual(final_path, destination)
            self.assertEqual(destination.read_bytes(), b"csv-bytes")
            self.assertEqual(
                session.calls[0]["url"],
                "https://fxg.jinritemai.com/shopuser/aftersale/export/download",
            )
            self.assertEqual(session.calls[0]["params"]["task_id"], "task-1")
            self.assertEqual(session.calls[0]["params"]["_lid"], "generated-lid")

    def test_download_export_uses_response_filename_when_available(self) -> None:
        service, _ = self.build_service(
            [
                FakeResponse(
                    content=b"xlsx-bytes",
                    headers={
                        "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "content-disposition": "attachment; filename=test-export.xlsx",
                    },
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "task-1.bin"
            final_path = service.download_export("task-1", destination)

            self.assertEqual(final_path.name, "test-export.xlsx")
            self.assertEqual(final_path.read_bytes(), b"xlsx-bytes")


if __name__ == "__main__":
    unittest.main()
