from __future__ import annotations

from datetime import datetime
import importlib
import importlib.util
import io
import unittest
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def format_local(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


class TimeProgressBarTests(unittest.TestCase):
    def load_module(self):
        spec = importlib.util.find_spec("aftersale_exporter.progress")
        self.assertIsNotNone(spec, "aftersale_exporter.progress module is missing")
        if spec is None:
            return None
        return importlib.import_module("aftersale_exporter.progress")

    def test_progress_bar_renders_initial_zero_and_full_on_download(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = io.StringIO()
        progress = module.TimeProgressBar(start_ts=10, end_ts=12, stream=output)

        self.assertIn("0.0%", output.getvalue())
        self.assertNotIn("0/3", output.getvalue())

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
        self.assertIn("100.0%", rendered)
        self.assertNotIn("3/3", rendered)
        self.assertIn(
            f"[downloaded] {format_local(10)}..{format_local(12)} -> /tmp/file.csv",
            rendered,
        )

    def test_progress_bar_uses_closed_range_seconds_for_uneven_splits(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = io.StringIO()
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
        self.assertIn("33.3%", output.getvalue())
        self.assertNotIn("2/6", output.getvalue())

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

        rendered = output.getvalue()
        self.assertIn("100.0%", rendered)
        self.assertNotIn("6/6", rendered)

    def test_progress_bar_keeps_submitted_status_on_same_line_while_waiting_for_generation(self) -> None:
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

        rendered = output.getvalue()
        self.assertNotIn("0/4", rendered)
        self.assertIn(
            f"submitted {format_local(5)}..{format_local(8)} task=task-5-8 | 等待文件生成 | 导出间隔 181s",
            rendered,
        )
        self.assertIn(
            f"submitted {format_local(5)}..{format_local(8)} task=task-5-8 | 等待文件生成 | 导出间隔 180s",
            rendered,
        )
        self.assertNotIn("[submitted]", rendered)
        self.assertNotIn("[waiting_export_gap]", rendered)

    def test_progress_bar_hides_not_ready_poll_result_from_status_line(self) -> None:
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

        rendered = output.getvalue()
        self.assertIn(
            f"submitted {format_local(5)}..{format_local(8)} task=task-5-8 | 等待文件生成 | 导出间隔 181s",
            rendered,
        )
        self.assertNotIn("结果：文件未生成", rendered)
        self.assertNotIn("[task_polled]", rendered)

    def test_progress_bar_updates_export_gap_countdown_without_extra_event_logs(self) -> None:
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

        rendered = output.getvalue()
        self.assertIn("等待导出请求间隔 181s", rendered)
        self.assertIn("等待导出请求间隔 180s", rendered)
        self.assertNotIn("0/4", rendered)
        self.assertNotIn("[waiting_export_gap]", rendered)

    def test_progress_bar_clears_old_export_gap_countdown_when_waiting_without_pending_segments(self) -> None:
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

        final_status = output.getvalue().split("\r")[-1]
        self.assertIn(
            f"submitted {format_local(5)}..{format_local(8)} task=task-5-8 | 等待文件生成",
            final_status,
        )
        self.assertNotIn("导出间隔", final_status)
        self.assertNotIn("结果：文件未生成", final_status)

    def test_progress_bar_keeps_completed_ratio_when_a_later_segment_fails(self) -> None:
        module = self.load_module()
        if module is None:
            return

        output = io.StringIO()
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

    def test_progress_bar_prints_event_logs_and_ends_with_newline(self) -> None:
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
        self.assertIn(f"submitted {format_local(8)}..{format_local(8)} task=task-8-8", rendered)
        self.assertNotIn("[submitted]", rendered)
        self.assertIn(
            f"[downloaded] {format_local(8)}..{format_local(8)} -> /tmp/export.csv",
            rendered,
        )
        self.assertTrue(rendered.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
