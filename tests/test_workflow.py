from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from aftersale_exporter.workflow import (
    AuthenticationError,
    ExportCoordinator,
    ExportCooldownError,
    OverLimitError,
    TaskPollResult,
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
        cooldown_failures: dict[tuple[int, int], int] | None = None,
        poll_sequences: dict[str, list[TaskPollResult | Exception]] | None = None,
        download_failure_tasks: set[str] | None = None,
    ) -> None:
        self.over_limit_ranges = over_limit_ranges or set()
        self.auth_failure_ranges = auth_failure_ranges or set()
        self.cooldown_failures = dict(cooldown_failures or {})
        self.poll_sequences = {key: list(value) for key, value in (poll_sequences or {}).items()}
        self.download_failure_tasks = download_failure_tasks or set()
        self.submissions: list[tuple[int, int]] = []
        self.polled_tasks: list[str] = []
        self.downloaded_tasks: list[str] = []

    def create_export(self, start_ts: int, end_ts: int) -> str:
        key = (start_ts, end_ts)
        self.submissions.append(key)
        if key in self.auth_failure_ranges:
            raise AuthenticationError("expired")
        remaining_cooldown_failures = self.cooldown_failures.get(key, 0)
        if remaining_cooldown_failures > 0:
            self.cooldown_failures[key] = remaining_cooldown_failures - 1
            raise ExportCooldownError("店铺3分钟内不允许再次导出，请稍后再试", retry_after_seconds=181)
        if key in self.over_limit_ranges:
            raise OverLimitError("too many rows")
        return f"task-{start_ts}-{end_ts}"

    def wait_for_task(
        self,
        task_id: str,
        poll_interval: float,
        timeout: float,
        status_callback=None,
    ) -> FakeTaskResult:
        raise AssertionError("workflow should use poll_task instead of wait_for_task")

    def poll_task(self, task_id: str) -> TaskPollResult:
        self.polled_tasks.append(task_id)
        sequence = self.poll_sequences.get(task_id)
        if sequence:
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return TaskPollResult(
            requested_at_ts=1234567890,
            result_text="文件已生成",
            is_complete=True,
            download_name=f"{task_id}.csv",
        )

    def download_export(self, task_id: str, destination: Path) -> Path:
        self.downloaded_tasks.append(task_id)
        if task_id in self.download_failure_tasks:
            raise RuntimeError("disk full")
        destination.write_text("id,value\n1,ok\n", encoding="utf-8")
        return destination


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ExportCoordinatorTests(unittest.TestCase):
    def test_single_interval_runs_without_split_when_under_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            events: list[tuple[str, dict[str, object]]] = []
            coordinator = ExportCoordinator(
                service=FakeService(),
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                event_callback=lambda event_name, payload: events.append((event_name, payload)),
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            result = coordinator.run(10, 19)

        self.assertEqual(result.segment_count, 1)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(
            [(segment.start_ts, segment.end_ts) for segment in result.segments],
            [(10, 19)],
        )
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(
            [event_name for event_name, _ in events],
            ["submitted", "waiting_task", "task_polled", "downloaded"],
        )
        task_polled_payload = [payload for event_name, payload in events if event_name == "task_polled"][0]
        self.assertEqual(task_polled_payload["requested_at_ts"], 1234567890)
        self.assertEqual(task_polled_payload["result_text"], "文件已生成")

    def test_over_limit_interval_is_split_and_rate_limited_between_export_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(over_limit_ranges={(0, 9)})
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            result = coordinator.run(0, 9)

        self.assertEqual(service.submissions, [(0, 9), (0, 4), (5, 9)])
        self.assertEqual(
            [(segment.start_ts, segment.end_ts) for segment in result.segments],
            [(0, 4), (5, 9)],
        )
        self.assertEqual(sum(clock.sleeps), 181.0)

    def test_waiting_generation_time_counts_toward_next_export_request_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(
                over_limit_ranges={(0, 3)},
                poll_sequences={
                    "task-0-1": [
                        TaskPollResult(
                            requested_at_ts=1,
                            result_text="文件未生成",
                            is_complete=False,
                            download_name=None,
                        ),
                        TaskPollResult(
                            requested_at_ts=201,
                            result_text="文件已生成",
                            is_complete=True,
                            download_name="task-0-1.csv",
                        ),
                    ],
                    "task-2-3": [
                        TaskPollResult(
                            requested_at_ts=202,
                            result_text="文件已生成",
                            is_complete=True,
                            download_name="task-2-3.csv",
                        )
                    ],
                },
            )
            events: list[tuple[str, dict[str, object]]] = []
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=200.0,
                task_timeout=1000.0,
                event_callback=lambda event_name, payload: events.append((event_name, payload)),
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            result = coordinator.run(0, 3)

        self.assertEqual(result.segment_count, 2)
        self.assertEqual(service.submissions, [(0, 3), (0, 1), (2, 3)])
        self.assertEqual(
            [event_name for event_name, _ in events].count("waiting_export_gap"),
            1,
        )
        self.assertEqual(clock.sleeps.count(1.0), 0)
        self.assertEqual(sum(clock.sleeps), 200.0)
        self.assertEqual(service.polled_tasks, ["task-0-1", "task-2-3", "task-0-1"])

    def test_waiting_generation_keeps_emitting_export_gap_before_next_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(
                over_limit_ranges={(0, 3)},
                poll_sequences={
                    "task-0-1": [
                        TaskPollResult(
                            requested_at_ts=0,
                            result_text="文件未生成",
                            is_complete=False,
                            download_name=None,
                        ),
                        TaskPollResult(
                            requested_at_ts=100,
                            result_text="文件已生成",
                            is_complete=True,
                            download_name="task-0-1.csv",
                        ),
                    ],
                    "task-2-3": [
                        TaskPollResult(
                            requested_at_ts=181,
                            result_text="文件已生成",
                            is_complete=True,
                            download_name="task-2-3.csv",
                        )
                    ],
                },
            )
            events: list[tuple[str, dict[str, object]]] = []
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=100.0,
                task_timeout=1000.0,
                event_callback=lambda event_name, payload: events.append((event_name, payload)),
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            result = coordinator.run(0, 3)

        self.assertEqual(result.segment_count, 2)
        self.assertEqual(service.polled_tasks, ["task-0-1", "task-0-1", "task-2-3"])
        self.assertEqual(
            [event_name for event_name, _ in events].count("waiting_export_gap"),
            1,
        )
        waiting_task_payload = [payload for event_name, payload in events if event_name == "waiting_task"][0]
        self.assertEqual(waiting_task_payload["export_gap_total_seconds"], 181)
        self.assertEqual(waiting_task_payload["export_gap_remaining_seconds"], 181)
        task_polled_payloads = [payload for event_name, payload in events if event_name == "task_polled"]
        self.assertEqual(task_polled_payloads[0]["export_gap_total_seconds"], 181)
        self.assertEqual(task_polled_payloads[0]["export_gap_remaining_seconds"], 181)

    def test_export_gap_starts_when_task_creation_succeeds(self) -> None:
        class SlowCreateService(FakeService):
            def __init__(self, *, clock: FakeClock) -> None:
                super().__init__()
                self.clock = clock
                self.submission_started_at: list[float] = []

            def create_export(self, start_ts: int, end_ts: int) -> str:
                self.submission_started_at.append(self.clock.monotonic())
                if (start_ts, end_ts) == (0, 1):
                    self.clock.now += 10.0
                return super().create_export(start_ts, end_ts)

        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = SlowCreateService(clock=clock)
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=20.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            service.poll_sequences = {
                "task-0-1": [
                    TaskPollResult(
                        requested_at_ts=10,
                        result_text="文件已生成",
                        is_complete=True,
                        download_name="task-0-1.csv",
                    )
                ],
                "task-2-3": [
                    TaskPollResult(
                        requested_at_ts=191,
                        result_text="文件已生成",
                        is_complete=True,
                        download_name="task-2-3.csv",
                    )
                ],
            }
            service.over_limit_ranges = {(0, 3)}

            result = coordinator.run(0, 3)

        self.assertEqual(result.segment_count, 2)
        self.assertEqual(service.submission_started_at, [0.0, 0.0, 191.0])
        self.assertEqual(service.submissions, [(0, 3), (0, 1), (2, 3)])

    def test_initial_export_cooldown_waits_and_retries_without_failed_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(cooldown_failures={(10, 19): 1})
            events: list[tuple[str, dict[str, object]]] = []
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                event_callback=lambda event_name, payload: events.append((event_name, payload)),
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            result = coordinator.run(10, 19)

        self.assertEqual(result.segment_count, 1)
        self.assertEqual(service.submissions, [(10, 19), (10, 19)])
        self.assertEqual(clock.sleeps.count(1.0), 0)
        self.assertEqual(clock.sleeps, [181.0])
        self.assertNotIn("failed", [event_name for event_name, _ in events])
        cooldown_payload = [
            payload for event_name, payload in events if event_name == "waiting_retry_cooldown"
        ][0]
        self.assertEqual(cooldown_payload["remaining_seconds"], 181)
        self.assertEqual(cooldown_payload["message"], "店铺3分钟内不允许再次导出，请稍后再试")

    def test_runtime_export_cooldown_extends_next_submit_time_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(
                over_limit_ranges={(0, 3)},
                cooldown_failures={(2, 3): 1},
                poll_sequences={
                    "task-0-1": [
                        TaskPollResult(
                            requested_at_ts=0,
                            result_text="文件已生成",
                            is_complete=True,
                            download_name="task-0-1.csv",
                        )
                    ],
                    "task-2-3": [
                        TaskPollResult(
                            requested_at_ts=362,
                            result_text="文件已生成",
                            is_complete=True,
                            download_name="task-2-3.csv",
                        )
                    ],
                },
            )
            events: list[tuple[str, dict[str, object]]] = []
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=10.0,
                event_callback=lambda event_name, payload: events.append((event_name, payload)),
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            result = coordinator.run(0, 3)

        self.assertEqual(result.segment_count, 2)
        self.assertEqual(service.submissions, [(0, 3), (0, 1), (2, 3), (2, 3)])
        self.assertEqual(sum(clock.sleeps), 362.0)
        self.assertEqual(clock.sleeps.count(1.0), 0)
        retry_events = [payload for event_name, payload in events if event_name == "waiting_retry_cooldown"]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["remaining_seconds"], 181)
        self.assertEqual(retry_events[0]["start_ts"], 2)
        self.assertEqual(retry_events[0]["end_ts"], 3)

    def test_task_timeout_starts_when_task_creation_succeeds(self) -> None:
        class SlowCreateService(FakeService):
            def __init__(self, *, clock: FakeClock) -> None:
                super().__init__()
                self.clock = clock

            def create_export(self, start_ts: int, end_ts: int) -> str:
                self.clock.now += 10.0
                return super().create_export(start_ts, end_ts)

        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = SlowCreateService(clock=clock)
            service.poll_sequences = {
                "task-0-1": [
                    TaskPollResult(
                        requested_at_ts=10,
                        result_text="文件未生成",
                        is_complete=False,
                        download_name=None,
                    ),
                    TaskPollResult(
                        requested_at_ts=25,
                        result_text="文件未生成",
                        is_complete=False,
                        download_name=None,
                    ),
                ]
            }
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=15.0,
                task_timeout=20.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            with self.assertRaisesRegex(TimeoutError, "task task-0-1 did not finish before timeout"):
                coordinator.run(0, 1)

        self.assertEqual(service.polled_tasks, ["task-0-1", "task-0-1"])
        self.assertEqual(clock.now, 25.0)

    def test_multiple_waiting_tasks_are_polled_and_first_completed_downloads_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(
                over_limit_ranges={(0, 5)},
                poll_sequences={
                    "task-0-2": [
                        TaskPollResult(
                            requested_at_ts=181,
                            result_text="文件未生成",
                            is_complete=False,
                            download_name=None,
                        ),
                        TaskPollResult(
                            requested_at_ts=281,
                            result_text="文件未生成",
                            is_complete=False,
                            download_name=None,
                        ),
                        TaskPollResult(
                            requested_at_ts=381,
                            result_text="文件已生成",
                            is_complete=True,
                            download_name="task-0-2.csv",
                        ),
                    ],
                    "task-3-5": [
                        TaskPollResult(
                            requested_at_ts=362,
                            result_text="文件已生成",
                            is_complete=True,
                            download_name="task-3-5.csv",
                        )
                    ],
                },
            )
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=100.0,
                task_timeout=1000.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            result = coordinator.run(0, 5)

        self.assertEqual(
            [(segment.start_ts, segment.end_ts) for segment in result.segments],
            [(0, 2), (3, 5)],
        )
        self.assertEqual(service.submissions, [(0, 5), (0, 2), (3, 5)])
        self.assertEqual(service.polled_tasks, ["task-0-2", "task-0-2", "task-3-5", "task-0-2"])
        self.assertEqual(service.downloaded_tasks, ["task-3-5", "task-0-2"])

    def test_poll_failure_is_recorded_after_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(
                poll_sequences={"task-2-3": [RuntimeError("poll failed")]},
            )
            events: list[tuple[str, dict[str, object]]] = []
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                event_callback=lambda event_name, payload: events.append((event_name, payload)),
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            with self.assertRaisesRegex(RuntimeError, "poll failed"):
                coordinator.run(2, 3)

        self.assertEqual(
            [event_name for event_name, _ in events],
            ["submitted", "waiting_task", "failed"],
        )
        self.assertEqual(service.downloaded_tasks, [])

    def test_single_second_interval_still_over_limit_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            coordinator = ExportCoordinator(
                service=FakeService(over_limit_ranges={(42, 42)}),
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            with self.assertRaisesRegex(OverLimitError, "single second"):
                coordinator.run(42, 42)

    def test_authentication_failure_stops_without_splitting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            service = FakeService(auth_failure_ranges={(100, 101)})
            coordinator = ExportCoordinator(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
            )

            with self.assertRaisesRegex(AuthenticationError, "expired"):
                coordinator.run(100, 101)

        self.assertEqual(service.submissions, [(100, 101)])


if __name__ == "__main__":
    unittest.main()
