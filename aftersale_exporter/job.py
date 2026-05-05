from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from aftersale_exporter.merge import merge_tabular_exports
from aftersale_exporter.workflow import ExportCoordinator, ExportRunResult


class ManifestTracker:
    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path
        self.summary: dict[str, Any] = {
            "segment_count": 0,
            "failed_count": 0,
        }
        self.segments: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.splits: list[dict[str, Any]] = []
        self.write()

    def handle(self, event_name: str, payload: dict[str, Any]) -> None:
        if event_name == "split":
            self.splits.append(dict(payload))
        elif event_name == "submitted":
            segment = self._find_or_create_segment(payload["start_ts"], payload["end_ts"])
            segment.update(
                {
                    "task_id": payload["task_id"],
                    "status": "submitted",
                }
            )
        elif event_name == "downloaded":
            segment = self._find_or_create_segment(payload["start_ts"], payload["end_ts"])
            segment.update(
                {
                    "task_id": payload["task_id"],
                    "file_path": payload["file_path"],
                    "status": "downloaded",
                }
            )
        elif event_name == "failed":
            segment = self._find_or_create_segment(payload["start_ts"], payload["end_ts"])
            segment.update(
                {
                    "task_id": payload.get("task_id"),
                    "status": "failed",
                }
            )
            self.failures.append(dict(payload))
            self.summary["failed_count"] = len(self.failures)
        self.summary["segment_count"] = len(self.segments)
        self.write()

    def finalize(self, result: ExportRunResult | None, merge_error: str | None = None) -> None:
        if result is not None:
            self.summary["segment_count"] = result.segment_count
            self.summary["failed_count"] = len(self.failures)
            for segment in result.segments:
                current = self._find_or_create_segment(segment.start_ts, segment.end_ts)
                current.update(
                    {
                        "task_id": segment.task_id,
                        "file_path": str(segment.file_path),
                        "status": current.get("status", "downloaded"),
                    }
                )
        if merge_error is not None:
            self.summary["merge_error"] = merge_error
        self.write()

    def write(self) -> None:
        payload = {
            "summary": self.summary,
            "segments": self.segments,
            "splits": self.splits,
            "failures": self.failures,
        }
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _find_or_create_segment(self, start_ts: int, end_ts: int) -> dict[str, Any]:
        for segment in self.segments:
            if segment["start_ts"] == start_ts and segment["end_ts"] == end_ts:
                return segment
        segment = {
            "start_ts": start_ts,
            "end_ts": end_ts,
        }
        self.segments.append(segment)
        return segment


class AftersaleExportJob:
    def __init__(
        self,
        *,
        service,
        out_dir: Path,
        poll_interval: float,
        task_timeout: float,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.service = service
        self.out_dir = out_dir
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.progress_callback = progress_callback
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def run(self, start_ts: int, end_ts: int) -> ExportRunResult:
        tracker = ManifestTracker(self.out_dir / "manifest.json")
        self._active_tracker = tracker
        coordinator = ExportCoordinator(
            service=self.service,
            out_dir=self.out_dir,
            poll_interval=self.poll_interval,
            task_timeout=self.task_timeout,
            event_callback=self._emit_event,
        )
        result: ExportRunResult | None = None
        merge_error: str | None = None

        try:
            try:
                result = coordinator.run(start_ts, end_ts)
                merge_tabular_exports(
                    [segment.file_path for segment in result.segments],
                    self.out_dir / "merged.xlsx",
                )
            except ValueError as exc:
                if result is not None:
                    merge_error = str(exc)
                else:
                    tracker.handle(
                        "failed",
                        {
                            "start_ts": start_ts,
                            "end_ts": end_ts,
                            "error_type": exc.__class__.__name__,
                            "message": str(exc),
                        },
                    )
                    raise
            except Exception:
                tracker.finalize(result, merge_error=merge_error)
                raise
            tracker.finalize(result, merge_error=merge_error)
        finally:
            self._active_tracker = None
        return result

    def _emit_event(self, event_name: str, payload: dict[str, Any]) -> None:
        tracker = getattr(self, "_active_tracker", None)
        if tracker is not None:
            tracker.handle(event_name, payload)
        if self.progress_callback is not None:
            self.progress_callback(event_name, payload)
