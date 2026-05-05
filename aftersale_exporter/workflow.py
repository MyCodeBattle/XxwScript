from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import math
import time
from typing import Any, Callable, Protocol

EXPORT_GAP_SECONDS = 181.0


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
class TaskPollResult:
    requested_at_ts: int
    result_text: str
    is_complete: bool
    download_name: str | None


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


@dataclass
class PendingSegment:
    start_ts: int
    end_ts: int


@dataclass
class ActiveExportTask:
    start_ts: int
    end_ts: int
    task_id: str
    deadline_at: float
    next_poll_at: float


@dataclass
class ReadyDownload:
    start_ts: int
    end_ts: int
    task_id: str
    download_name: str


class ExportService(Protocol):
    def create_export(self, start_ts: int, end_ts: int) -> str: ...

    def poll_task(self, task_id: str) -> TaskPollResult: ...

    def wait_for_task(
        self,
        task_id: str,
        poll_interval: float,
        timeout: float,
        status_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> TaskResult: ...

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
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self.service = service
        self.out_dir = out_dir
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.event_callback = event_callback
        self.sleep_fn = time.sleep if sleep_fn is None else sleep_fn
        self.time_fn = time.monotonic if time_fn is None else time_fn
        self.raw_dir = out_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._last_export_request_at: float | None = None

    def run(self, start_ts: int, end_ts: int) -> ExportRunResult:
        self._last_export_request_at = None
        segments = self._run_scheduler(start_ts, end_ts)
        ordered_segments = sorted(segments, key=lambda item: (item.start_ts, item.end_ts))
        return ExportRunResult(segments=ordered_segments, failed_count=0)

    def _run_scheduler(self, start_ts: int, end_ts: int) -> list[ExportSegment]:
        pending_segments: deque[PendingSegment] = deque([PendingSegment(start_ts, end_ts)])
        active_tasks: dict[str, ActiveExportTask] = {}
        ready_downloads: deque[ReadyDownload] = deque()
        completed_segments: list[ExportSegment] = []

        while pending_segments or active_tasks or ready_downloads:
            if ready_downloads:
                ready = ready_downloads.popleft()
                completed_segments.append(self._download_ready_task(ready))
                continue

            now = self.time_fn()
            due_tasks = [
                task
                for task in active_tasks.values()
                if task.next_poll_at <= now
            ]
            if due_tasks:
                due_tasks.sort(key=lambda item: (item.next_poll_at, item.start_ts, item.end_ts))
                for task in due_tasks:
                    ready = self._poll_active_task(task)
                    if ready is None:
                        continue
                    active_tasks.pop(task.task_id, None)
                    ready_downloads.append(ready)
                continue

            if pending_segments and self._can_submit_export_request(now):
                segment = pending_segments.popleft()
                submitted_task = self._submit_pending_segment(segment, pending_segments)
                if submitted_task is not None:
                    active_tasks[submitted_task.task_id] = submitted_task
                continue

            self._sleep_until_next_action(pending_segments, active_tasks)

        return completed_segments

    def _submit_pending_segment(
        self,
        segment: PendingSegment,
        pending_segments: deque[PendingSegment],
    ) -> ActiveExportTask | None:
        request_started_at = self.time_fn()
        self._last_export_request_at = request_started_at
        try:
            task_id = self.service.create_export(segment.start_ts, segment.end_ts)
            self._emit(
                "submitted",
                {
                    "start_ts": segment.start_ts,
                    "end_ts": segment.end_ts,
                    "task_id": task_id,
                },
            )
        except OverLimitError as exc:
            if segment.start_ts == segment.end_ts:
                self._emit(
                    "failed",
                    {
                        "start_ts": segment.start_ts,
                        "end_ts": segment.end_ts,
                        "error_type": "over_limit",
                        "message": str(exc),
                    },
                )
                raise OverLimitError(
                    f"single second interval {segment.start_ts} still exceeds platform limit"
                ) from exc
            mid = (segment.start_ts + segment.end_ts) // 2
            self._emit(
                "split",
                {
                    "start_ts": segment.start_ts,
                    "end_ts": segment.end_ts,
                    "midpoint": mid,
                },
            )
            pending_segments.appendleft(PendingSegment(mid + 1, segment.end_ts))
            pending_segments.appendleft(PendingSegment(segment.start_ts, mid))
            return None
        except Exception as exc:
            self._emit(
                "failed",
                {
                    "start_ts": segment.start_ts,
                    "end_ts": segment.end_ts,
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                },
            )
            raise

        self._emit(
            "waiting_task",
            {
                "start_ts": segment.start_ts,
                "end_ts": segment.end_ts,
                "task_id": task_id,
                "poll_interval": self.poll_interval,
                "timeout": self.task_timeout,
            },
        )
        return ActiveExportTask(
            start_ts=segment.start_ts,
            end_ts=segment.end_ts,
            task_id=task_id,
            deadline_at=request_started_at + self.task_timeout,
            next_poll_at=request_started_at,
        )

    def _poll_active_task(self, task: ActiveExportTask) -> ReadyDownload | None:
        try:
            poll_result = self.service.poll_task(task.task_id)
        except Exception as exc:
            self._emit(
                "failed",
                {
                    "start_ts": task.start_ts,
                    "end_ts": task.end_ts,
                    "task_id": task.task_id,
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                },
            )
            raise

        self._emit(
            "task_polled",
            {
                "start_ts": task.start_ts,
                "end_ts": task.end_ts,
                "task_id": task.task_id,
                "requested_at_ts": poll_result.requested_at_ts,
                "result_text": poll_result.result_text,
            },
        )
        if poll_result.is_complete:
            download_name = poll_result.download_name or f"{task.task_id}.bin"
            return ReadyDownload(
                start_ts=task.start_ts,
                end_ts=task.end_ts,
                task_id=task.task_id,
                download_name=download_name,
            )

        now = self.time_fn()
        if now + self.poll_interval > task.deadline_at:
            timeout_error = TimeoutError(f"task {task.task_id} did not finish before timeout")
            self._emit(
                "failed",
                {
                    "start_ts": task.start_ts,
                    "end_ts": task.end_ts,
                    "task_id": task.task_id,
                    "error_type": timeout_error.__class__.__name__,
                    "message": str(timeout_error),
                },
            )
            raise timeout_error
        task.next_poll_at = now + self.poll_interval
        return None

    def _download_ready_task(self, ready: ReadyDownload) -> ExportSegment:
        try:
            destination = self.raw_dir / ready.download_name
            final_path = self.service.download_export(ready.task_id, destination)
        except Exception as exc:
            self._emit(
                "failed",
                {
                    "start_ts": ready.start_ts,
                    "end_ts": ready.end_ts,
                    "task_id": ready.task_id,
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                },
            )
            raise

        self._emit(
            "downloaded",
            {
                "start_ts": ready.start_ts,
                "end_ts": ready.end_ts,
                "task_id": ready.task_id,
                "file_path": str(final_path),
            },
        )
        return ExportSegment(
            start_ts=ready.start_ts,
            end_ts=ready.end_ts,
            task_id=ready.task_id,
            file_path=final_path,
        )

    def _can_submit_export_request(self, now: float) -> bool:
        if self._last_export_request_at is None:
            return True
        return now >= self._last_export_request_at + EXPORT_GAP_SECONDS

    def _sleep_until_next_action(
        self,
        pending_segments: deque[PendingSegment],
        active_tasks: dict[str, ActiveExportTask],
    ) -> None:
        now = self.time_fn()
        next_submit_at = math.inf
        if pending_segments:
            if self._last_export_request_at is None:
                return
            next_submit_at = self._last_export_request_at + EXPORT_GAP_SECONDS

        next_poll_at = min((task.next_poll_at for task in active_tasks.values()), default=math.inf)
        next_action_at = min(next_submit_at, next_poll_at)
        if math.isinf(next_action_at):
            return

        if next_submit_at < next_poll_at:
            remaining_seconds = max(0, int(math.ceil(next_submit_at - now)))
            if remaining_seconds > 0:
                self._emit(
                    "waiting_export_gap",
                    {
                        "total_seconds": int(EXPORT_GAP_SECONDS),
                        "remaining_seconds": remaining_seconds,
                    },
                )
            self.sleep_fn(min(1.0, max(0.0, next_submit_at - now)))
            return

        sleep_seconds = max(0.0, next_action_at - now)
        if sleep_seconds > 0:
            self.sleep_fn(sleep_seconds)

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback(event_name, payload)
