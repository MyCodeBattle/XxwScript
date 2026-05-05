from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from aftersale_exporter.workflow import (
    AuthenticationError,
    ExportCoordinator,
    OverLimitError,
)


@dataclass
class FakeTaskResult:
    download_name: str


class FakeService:
    def __init__(
        self,
        *,
        over_limit_ranges: set[tuple[int, int]] | None = None,
        auth_failure_ranges: set[tuple[int, int]] | None = None,
        wait_failure_ranges: set[tuple[int, int]] | None = None,
    ) -> None:
        self.over_limit_ranges = over_limit_ranges or set()
        self.auth_failure_ranges = auth_failure_ranges or set()
        self.wait_failure_ranges = wait_failure_ranges or set()
        self.submissions: list[tuple[int, int]] = []
        self.waited_tasks: list[str] = []
        self.downloaded_tasks: list[str] = []

    def create_export(self, start_ts: int, end_ts: int) -> str:
        key = (start_ts, end_ts)
        self.submissions.append(key)
        if key in self.auth_failure_ranges:
            raise AuthenticationError("expired")
        if key in self.over_limit_ranges:
            raise OverLimitError("too many rows")
        return f"task-{start_ts}-{end_ts}"

    def wait_for_task(self, task_id: str, poll_interval: float, timeout: float) -> FakeTaskResult:
        self.waited_tasks.append(task_id)
        _, start_ts, end_ts = task_id.split("-", 2)
        if (int(start_ts), int(end_ts)) in self.wait_failure_ranges:
            raise RuntimeError("wait failed")
        return FakeTaskResult(download_name=f"{task_id}.csv")

    def download_export(self, task_id: str, destination: Path) -> Path:
        self.downloaded_tasks.append(task_id)
        destination.write_text("id,value\n1,ok\n", encoding="utf-8")
        return destination


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class ExportCoordinatorTests(unittest.TestCase):
    def test_single_interval_runs_without_split_when_under_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            coordinator = ExportCoordinator(
                service=FakeService(),
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
            )

            result = coordinator.run(10, 19)

        self.assertEqual(result.segment_count, 1)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(
            [(segment.start_ts, segment.end_ts) for segment in result.segments],
            [(10, 19)],
        )
        self.assertEqual(clock.sleeps, [])

    def test_over_limit_interval_is_split_into_closed_second_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(over_limit_ranges={(0, 9)})
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
            )

            result = coordinator.run(0, 9)

        self.assertEqual(service.submissions, [(0, 9), (0, 4), (5, 9)])
        self.assertEqual(
            [(segment.start_ts, segment.end_ts) for segment in result.segments],
            [(0, 4), (5, 9)],
        )
        self.assertEqual(clock.sleeps, [181])

    def test_multiple_successful_downloads_sleep_between_each_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(over_limit_ranges={(0, 5), (0, 2)})
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
            )

            result = coordinator.run(0, 5)

        self.assertEqual(
            [(segment.start_ts, segment.end_ts) for segment in result.segments],
            [(0, 1), (2, 2), (3, 5)],
        )
        self.assertEqual(clock.sleeps, [181, 181])

    def test_single_second_interval_still_over_limit_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            coordinator = ExportCoordinator(
                service=FakeService(over_limit_ranges={(42, 42)}),
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
            )

            with self.assertRaisesRegex(OverLimitError, "single second"):
                coordinator.run(42, 42)

    def test_authentication_failure_stops_without_splitting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = FakeService(auth_failure_ranges={(100, 101)})
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
            )

            with self.assertRaisesRegex(AuthenticationError, "expired"):
                coordinator.run(100, 101)

        self.assertEqual(service.submissions, [(100, 101)])

    def test_failed_followup_branch_does_not_sleep_before_nonexistent_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(
                over_limit_ranges={(0, 3)},
                wait_failure_ranges={(2, 3)},
            )
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
            )

            with self.assertRaisesRegex(RuntimeError, "wait failed"):
                coordinator.run(0, 3)

        self.assertEqual(service.downloaded_tasks, ["task-0-1"])
        self.assertEqual(clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
