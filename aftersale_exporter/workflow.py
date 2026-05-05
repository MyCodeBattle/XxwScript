from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable, Protocol

DOWNLOAD_GAP_SECONDS = 181.0


class ExportError(Exception):
    """Base export error."""


class OverLimitError(ExportError):
    """Raised when export exceeds platform row limit."""


class AuthenticationError(ExportError):
    """Raised when authentication has expired."""


@dataclass(frozen=True)
class TaskResult:
    download_name: str


@dataclass(frozen=True)
class ExportSegment:
    start_ts: int
    end_ts: int
    task_id: str
    file_path: Path


@dataclass(frozen=True)
class ExportRunResult:
    segments: list[ExportSegment] = field(default_factory=list)
    failed_count: int = 0

    @property
    def segment_count(self) -> int:
        return len(self.segments)


class ExportService(Protocol):
    def create_export(self, start_ts: int, end_ts: int) -> str: ...

    def wait_for_task(self, task_id: str, poll_interval: float, timeout: float) -> TaskResult: ...

    def download_export(self, task_id: str, destination: Path) -> Path: ...


class ExportCoordinator:
    def __init__(
        self,
        *,
        service: ExportService,
        out_dir: Path,
        poll_interval: float,
        task_timeout: float,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.service = service
        self.out_dir = out_dir
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.event_callback = event_callback
        self.sleep_fn = time.sleep if sleep_fn is None else sleep_fn
        self.raw_dir = out_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._has_downloaded_segment = False

    def run(self, start_ts: int, end_ts: int) -> ExportRunResult:
        self._has_downloaded_segment = False
        segments = self._export_range(start_ts, end_ts)
        return ExportRunResult(segments=segments, failed_count=0)

    def _export_range(self, start_ts: int, end_ts: int) -> list[ExportSegment]:
        try:
            task_id = self.service.create_export(start_ts, end_ts)
            self._emit(
                "submitted",
                {
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "task_id": task_id,
                },
            )
        except OverLimitError as exc:
            if start_ts == end_ts:
                self._emit(
                    "failed",
                    {
                        "start_ts": start_ts,
                        "end_ts": end_ts,
                        "error_type": "over_limit",
                        "message": str(exc),
                    },
                )
                raise OverLimitError(
                    f"single second interval {start_ts} still exceeds platform limit"
                ) from exc
            mid = (start_ts + end_ts) // 2
            self._emit(
                "split",
                {
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "midpoint": mid,
                },
            )
            left = self._export_range(start_ts, mid)
            right = self._export_range(mid + 1, end_ts)
            return left + right
        except Exception as exc:
            self._emit(
                "failed",
                {
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                },
            )
            raise

        try:
            task_result = self.service.wait_for_task(
                task_id,
                poll_interval=self.poll_interval,
                timeout=self.task_timeout,
            )
            if self._has_downloaded_segment:
                self.sleep_fn(DOWNLOAD_GAP_SECONDS)
            destination = self.raw_dir / task_result.download_name
            final_path = self.service.download_export(task_id, destination)
        except Exception as exc:
            self._emit(
                "failed",
                {
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "task_id": task_id,
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                },
            )
            raise
        self._has_downloaded_segment = True
        self._emit(
            "downloaded",
            {
                "start_ts": start_ts,
                "end_ts": end_ts,
                "task_id": task_id,
                "file_path": str(final_path),
            },
        )
        return [
            ExportSegment(
                start_ts=start_ts,
                end_ts=end_ts,
                task_id=task_id,
                file_path=final_path,
            )
        ]

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback(event_name, payload)
