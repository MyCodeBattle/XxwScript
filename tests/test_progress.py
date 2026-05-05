from __future__ import annotations

import importlib
import importlib.util
import io
import unittest


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
        self.assertIn("0/3", output.getvalue())

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
        self.assertIn("3/3", rendered)
        self.assertIn("[downloaded] 10..12 -> /tmp/file.csv", rendered)

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
        self.assertIn("2/6", output.getvalue())

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
        self.assertIn("6/6", rendered)

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
        self.assertIn("2/4", rendered)
        self.assertIn("[failed] 2..3 RuntimeError: disk full", rendered)

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
        self.assertIn("[submitted] 8..8 task=task-8-8", rendered)
        self.assertIn("[downloaded] 8..8 -> /tmp/export.csv", rendered)
        self.assertTrue(rendered.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
