from __future__ import annotations

from datetime import datetime
import importlib
import importlib.util
import io
import unittest
from unittest import mock
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def format_local(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


class FakeTTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


class TimeProgressBarTests(unittest.TestCase):
    def load_module(self):
        spec = importlib.util.find_spec("aftersale_exporter.progress")
        self.assertIsNotNone(spec, "aftersale_exporter.progress module is missing")
        if spec is None:
            return None
        return importlib.import_module("aftersale_exporter.progress")

    def test_plain_progress_bar_skips_initial_waiting_and_logs_download_event(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = io.StringIO()
        progress = module.TimeProgressBar(start_ts=10, end_ts=12, stream=output)

        self.assertEqual(output.getvalue(), "")

        progress.handle_event(
            "downloaded",
            {
                "start_ts": 10,
                "end_ts": 12,
                "task_id": "task-10-12",
                "file_path": "/tmp/file.csv",
            },
        )
        progress.finish(success=True)

        rendered = output.getvalue()
        self.assertIn(
            f"[downloaded] {format_local(10)}..{format_local(12)} -> /tmp/file.csv",
            rendered,
        )
        self.assertNotIn("\r", rendered)

    def test_live_progress_bar_uses_closed_range_seconds_for_uneven_splits(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = FakeTTYStream()
        with mock.patch.dict("os.environ", {"TERM": "xterm"}, clear=False):
            progress = module.TimeProgressBar(start_ts=0, end_ts=5, stream=output)

            progress.handle_event(
                "downloaded",
                {
                    "start_ts": 0,
                    "end_ts": 1,
                    "task_id": "task-0-1",
                    "file_path": "/tmp/left.csv",
                },
            )
            progress.handle_event(
                "downloaded",
                {
                    "start_ts": 2,
                    "end_ts": 5,
                    "task_id": "task-2-5",
                    "file_path": "/tmp/right.csv",
                },
            )
            progress.finish(success=True)

        self.assertIn("33.3%", output.getvalue())
        self.assertNotIn("2/6", output.getvalue())

        rendered = output.getvalue()
        self.assertIn("100.0%", rendered)
        self.assertNotIn("6/6", rendered)

    def test_plain_progress_bar_logs_submitted_and_first_waiting_state_once(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = io.StringIO()
        progress = module.TimeProgressBar(start_ts=5, end_ts=8, stream=output)

        progress.handle_event(
            "submitted",
            {
                "start_ts": 5,
                "end_ts": 8,
                "task_id": "task-5-8",
            },
        )
        progress.handle_event(
            "waiting_task",
            {
                "start_ts": 5,
                "end_ts": 8,
                "task_id": "task-5-8",
                "export_gap_total_seconds": 181,
                "export_gap_remaining_seconds": 181,
            },
        )
        progress.handle_event(
            "waiting_export_gap",
            {
                "start_ts": 5,
                "end_ts": 8,
                "task_id": "task-5-8",
                "total_seconds": 181,
                "remaining_seconds": 180,
            },
        )
        progress.handle_event(
            "task_polled",
            {
                "start_ts": 5,
                "end_ts": 8,
                "task_id": "task-5-8",
                "requested_at_ts": 100,
                "result_text": "文件未生成",
                "export_gap_total_seconds": 181,
                "export_gap_remaining_seconds": 181,
            },
        )

        self.assertEqual(
            output.getvalue().splitlines(),
            [
                f"submitted {format_local(5)}..{format_local(8)} task=task-5-8",
                f"submitted {format_local(5)}..{format_local(8)} task=task-5-8 | 等待文件生成",
            ],
        )

    def test_live_progress_bar_keeps_submitted_status_on_same_line_while_waiting_for_generation(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = FakeTTYStream()
        with mock.patch.dict("os.environ", {"TERM": "xterm"}, clear=False):
            progress = module.TimeProgressBar(start_ts=5, end_ts=8, stream=output)

            progress.handle_event(
                "submitted",
                {
                    "start_ts": 5,
                    "end_ts": 8,
                    "task_id": "task-5-8",
                },
            )
            progress.handle_event(
                "waiting_task",
                {
                    "start_ts": 5,
                    "end_ts": 8,
                    "task_id": "task-5-8",
                    "export_gap_total_seconds": 181,
                    "export_gap_remaining_seconds": 181,
                },
            )
            progress.handle_event(
                "waiting_export_gap",
                {
                    "start_ts": 5,
                    "end_ts": 8,
                    "task_id": "task-5-8",
                    "total_seconds": 181,
                    "remaining_seconds": 180,
                },
            )

        rendered = output.getvalue()
        self.assertIn(
            f"submitted {format_local(5)}..{format_local(8)} task=task-5-8 | 等待文件生成 | 导出间隔 181s",
            rendered,
        )
        self.assertIn(
            f"submitted {format_local(5)}..{format_local(8)} task=task-5-8 | 等待文件生成 | 导出间隔 180s",
            rendered,
        )
        self.assertIn("\r", rendered)
        self.assertNotIn("[submitted]", rendered)
        self.assertNotIn("[waiting_export_gap]", rendered)

    def test_plain_progress_bar_ignores_waiting_export_gap_and_not_ready_poll_updates(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = io.StringIO()
        progress = module.TimeProgressBar(start_ts=0, end_ts=3, stream=output)

        progress.handle_event(
            "waiting_export_gap",
            {
                "start_ts": 2,
                "end_ts": 3,
                "task_id": "task-2-3",
                "total_seconds": 181,
                "remaining_seconds": 181,
            },
        )
        progress.handle_event(
            "waiting_export_gap",
            {
                "start_ts": 2,
                "end_ts": 3,
                "task_id": "task-2-3",
                "total_seconds": 181,
                "remaining_seconds": 180,
            },
        )

        self.assertEqual(output.getvalue(), "")

    def test_plain_progress_bar_ignores_not_ready_poll_updates_after_waiting(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = io.StringIO()
        progress = module.TimeProgressBar(start_ts=5, end_ts=8, stream=output)

        progress.handle_event(
            "submitted",
            {
                "start_ts": 5,
                "end_ts": 8,
                "task_id": "task-5-8",
            },
        )
        progress.handle_event(
            "waiting_task",
            {
                "start_ts": 5,
                "end_ts": 8,
                "task_id": "task-5-8",
                "export_gap_total_seconds": 181,
                "export_gap_remaining_seconds": 181,
            },
        )
        progress.handle_event(
            "task_polled",
            {
                "start_ts": 5,
                "end_ts": 8,
                "task_id": "task-5-8",
                "requested_at_ts": 100,
                "result_text": "文件未生成",
            },
        )

        self.assertEqual(
            output.getvalue().splitlines(),
            [
                f"submitted {format_local(5)}..{format_local(8)} task=task-5-8",
                f"submitted {format_local(5)}..{format_local(8)} task=task-5-8 | 等待文件生成",
            ],
        )
        self.assertNotIn("结果：文件未生成", output.getvalue())

    def test_live_progress_bar_keeps_completed_ratio_when_a_later_segment_fails(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = FakeTTYStream()
        with mock.patch.dict("os.environ", {"TERM": "xterm"}, clear=False):
            progress = module.TimeProgressBar(start_ts=0, end_ts=3, stream=output)

            progress.handle_event(
                "downloaded",
                {
                    "start_ts": 0,
                    "end_ts": 1,
                    "task_id": "task-0-1",
                    "file_path": "/tmp/left.csv",
                },
            )
            progress.handle_event(
                "failed",
                {
                    "start_ts": 2,
                    "end_ts": 3,
                    "task_id": "task-2-3",
                    "error_type": "RuntimeError",
                    "message": "disk full",
                },
            )
            progress.finish(success=False)

        rendered = output.getvalue()
        self.assertIn("50.0%", rendered)
        self.assertNotIn("2/4", rendered)
        self.assertIn(
            f"[failed] {format_local(2)}..{format_local(3)} RuntimeError: disk full",
            rendered,
        )

    def test_plain_progress_bar_prints_event_logs_and_ends_with_newline(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = io.StringIO()
        progress = module.TimeProgressBar(start_ts=8, end_ts=8, stream=output)

        progress.handle_event(
            "submitted",
            {
                "start_ts": 8,
                "end_ts": 8,
                "task_id": "task-8-8",
            },
        )
        progress.handle_event(
            "downloaded",
            {
                "start_ts": 8,
                "end_ts": 8,
                "task_id": "task-8-8",
                "file_path": "/tmp/export.csv",
            },
        )
        progress.finish(success=True)

        rendered = output.getvalue()
        self.assertEqual(
            rendered.splitlines()[0],
            f"submitted {format_local(8)}..{format_local(8)} task=task-8-8",
        )
        self.assertIn(
            f"[downloaded] {format_local(8)}..{format_local(8)} -> /tmp/export.csv",
            rendered,
        )
        self.assertTrue(rendered.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
