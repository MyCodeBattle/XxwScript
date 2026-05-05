from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from aftersale_exporter.job import AftersaleExportJob
from aftersale_exporter.workflow import OverLimitError


class MixedFormatFakeService:
    def __init__(self) -> None:
        self.submissions: list[tuple[int, int]] = []

    def create_export(self, start_ts: int, end_ts: int) -> str:
        self.submissions.append((start_ts, end_ts))
        if (start_ts, end_ts) == (0, 3):
            raise OverLimitError("too many rows")
        return f"task-{start_ts}-{end_ts}"

    def wait_for_task(self, task_id: str, poll_interval: float, timeout: float):
        if task_id == "task-0-1":
            return type("TaskResult", (), {"download_name": "left.csv"})()
        return type("TaskResult", (), {"download_name": "right.xlsx"})()

    def download_export(self, task_id: str, destination: Path) -> Path:
        if destination.suffix == ".csv":
            destination.write_text("id,left_only\n1,L\n", encoding="utf-8")
            return destination

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["id", "right_only"])
        sheet.append([2, "R"])
        workbook.save(destination)
        return destination


class UnsupportedFormatFakeService:
    def create_export(self, start_ts: int, end_ts: int) -> str:
        return f"task-{start_ts}-{end_ts}"

    def wait_for_task(self, task_id: str, poll_interval: float, timeout: float):
        return type("TaskResult", (), {"download_name": "bad.txt"})()

    def download_export(self, task_id: str, destination: Path) -> Path:
        destination.write_text("not-a-tabular-export", encoding="utf-8")
        return destination


class PartialFailureFakeService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_export(self, start_ts: int, end_ts: int) -> str:
        self.calls.append(f"create:{start_ts}-{end_ts}")
        if (start_ts, end_ts) == (0, 3):
            raise OverLimitError("too many rows")
        return f"task-{start_ts}-{end_ts}"

    def wait_for_task(self, task_id: str, poll_interval: float, timeout: float):
        return type("TaskResult", (), {"download_name": f"{task_id}.csv"})()

    def download_export(self, task_id: str, destination: Path) -> Path:
        if task_id == "task-2-3":
            raise RuntimeError("disk full")
        destination.write_text("id,value\n1,ok\n", encoding="utf-8")
        return destination


class AftersaleExportJobTests(unittest.TestCase):
    def test_job_writes_manifest_and_merged_xlsx_for_split_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job = AftersaleExportJob(
                service=MixedFormatFakeService(),
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
            )

            with patch("aftersale_exporter.workflow.time.sleep"):
                result = job.run(start_ts=0, end_ts=3)

            manifest_path = Path(tmpdir) / "manifest.json"
            merged_path = Path(tmpdir) / "merged.xlsx"
            raw_dir = Path(tmpdir) / "raw"

            self.assertTrue(manifest_path.exists())
            self.assertTrue(merged_path.exists())
            self.assertTrue((raw_dir / "left.csv").exists())
            self.assertTrue((raw_dir / "right.xlsx").exists())
            self.assertEqual(result.segment_count, 2)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["summary"]["segment_count"], 2)
            self.assertEqual(manifest["summary"]["failed_count"], 0)
            self.assertEqual(
                [(item["start_ts"], item["end_ts"]) for item in manifest["segments"]],
                [(0, 1), (2, 3)],
            )

            workbook = load_workbook(merged_path)
            rows = list(workbook.active.iter_rows(values_only=True))
            self.assertEqual(rows[0], ("id", "left_only", "right_only"))
            self.assertEqual(rows[1], ("1", "L", None))
            self.assertEqual(rows[2], (2, None, "R"))

    def test_job_keeps_raw_files_and_records_merge_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job = AftersaleExportJob(
                service=UnsupportedFormatFakeService(),
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
            )

            with patch("aftersale_exporter.workflow.time.sleep"):
                result = job.run(start_ts=10, end_ts=10)

            manifest = json.loads(
                (Path(tmpdir) / "manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result.segment_count, 1)
            self.assertTrue((Path(tmpdir) / "raw" / "bad.txt").exists())
            self.assertFalse((Path(tmpdir) / "merged.xlsx").exists())
            self.assertIn("merge_error", manifest["summary"])
            self.assertIn("Unsupported file type", manifest["summary"]["merge_error"])

    def test_job_persists_manifest_before_raising_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job = AftersaleExportJob(
                service=PartialFailureFakeService(),
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
            )

            with patch("aftersale_exporter.workflow.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    job.run(start_ts=0, end_ts=3)

            manifest = json.loads(
                (Path(tmpdir) / "manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(manifest["summary"]["failed_count"], 1)
            self.assertEqual(
                [(item["start_ts"], item["end_ts"], item["status"]) for item in manifest["segments"]],
                [(0, 1, "downloaded"), (2, 3, "failed")],
            )
            self.assertEqual(manifest["failures"][0]["message"], "disk full")

    def test_job_emits_progress_events(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            job = AftersaleExportJob(
                service=MixedFormatFakeService(),
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                progress_callback=lambda event_name, payload: events.append(event_name),
            )

            with patch("aftersale_exporter.workflow.time.sleep"):
                job.run(start_ts=0, end_ts=3)

        self.assertEqual(
            events,
            ["split", "submitted", "downloaded", "submitted", "downloaded"],
        )


if __name__ == "__main__":
    unittest.main()
