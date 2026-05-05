from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import math
import time
from typing import Any, Callable, Protocol

EXPORT_GAP_SECONDS = 181.0
TASK_TIMEOUT_RETRY_LIMIT = 1


class ExportError(Exception):
    """Base export error."""


class OverLimitError(ExportError):
    """Raised when export exceeds platform row limit."""


class AuthenticationError(ExportError):
    """Raised when authentication has expired."""


class ExportCooldownError(ExportError):
    """Raised when the platform requires waiting before the next export."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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
    timeout_retries: int = 0


@dataclass
class ActiveExportTask:
    start_ts: int
    end_ts: int
    task_id: str
    deadline_at: float
    next_poll_at: float
    timeout_retries: int = 0


@dataclass
class ReadyDownload:
    start_ts: int
    end_ts: int
    task_id: str
    download_name: str


@dataclass(frozen=True)
class TaskPollOutcome:
    ready_download: ReadyDownload | None = None
    retry_segment: PendingSegment | None = None
    task_failed: bool = False


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
        self._export_cooldown_until: float | None = None

    def run(self, start_ts: int, end_ts: int) -> ExportRunResult:
        self._last_export_request_at = None
        self._export_cooldown_until = None
        segments, failed_count = self._run_scheduler(start_ts, end_ts)
        ordered_segments = sorted(segments, key=lambda item: (item.start_ts, item.end_ts))
        return ExportRunResult(segments=ordered_segments, failed_count=failed_count)

    def _run_scheduler(self, start_ts: int, end_ts: int) -> tuple[list[ExportSegment], int]:
        pending_segments: deque[PendingSegment] = deque([PendingSegment(start_ts, end_ts)])
        active_tasks: dict[str, ActiveExportTask] = {}
        ready_downloads: deque[ReadyDownload] = deque()
        completed_segments: list[ExportSegment] = []
        failed_count = 0

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
                    outcome = self._poll_active_task(task, pending_segments)
                    if outcome.ready_download is not None:
                        active_tasks.pop(task.task_id, None)
                        ready_downloads.append(outcome.ready_download)
                        continue
                    if outcome.retry_segment is not None:
                        active_tasks.pop(task.task_id, None)
                        pending_segments.appendleft(outcome.retry_segment)
                        continue
                    if outcome.task_failed:
                        active_tasks.pop(task.task_id, None)
                        failed_count += 1
                continue

            if pending_segments and self._can_submit_export_request(now):
                segment = pending_segments.popleft()
                submitted_task = self._submit_pending_segment(segment, pending_segments)
                if submitted_task is not None:
                    active_tasks[submitted_task.task_id] = submitted_task
                continue

            self._sleep_until_next_action(pending_segments, active_tasks)

        return completed_segments, failed_count

    def _submit_pending_segment(
        self,
        segment: PendingSegment,
        pending_segments: deque[PendingSegment],
    ) -> ActiveExportTask | None:
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
        except ExportCooldownError as exc:
            retry_started_at = self.time_fn()
            self._export_cooldown_until = retry_started_at + exc.retry_after_seconds
            pending_segments.appendleft(segment)
            self._emit(
                "waiting_retry_cooldown",
                {
                    "start_ts": segment.start_ts,
                    "end_ts": segment.end_ts,
                    "remaining_seconds": exc.retry_after_seconds,
                    "message": str(exc),
                },
            )
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

        submitted_at = self.time_fn()
        self._last_export_request_at = submitted_at
        self._emit(
            "waiting_task",
            {
                "start_ts": segment.start_ts,
                "end_ts": segment.end_ts,
                "task_id": task_id,
                "poll_interval": self.poll_interval,
                "timeout": self.task_timeout,
                **self._build_export_gap_payload(pending_segments, now=submitted_at),
            },
        )
        return ActiveExportTask(
            start_ts=segment.start_ts,
            end_ts=segment.end_ts,
            task_id=task_id,
            deadline_at=submitted_at + self.task_timeout,
            next_poll_at=submitted_at,
            timeout_retries=segment.timeout_retries,
        )

    def _poll_active_task(
        self,
        task: ActiveExportTask,
        pending_segments: deque[PendingSegment],
    ) -> TaskPollOutcome:
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

        poll_payload = {
            "start_ts": task.start_ts,
            "end_ts": task.end_ts,
            "task_id": task.task_id,
            "requested_at_ts": poll_result.requested_at_ts,
            "result_text": poll_result.result_text,
        }
        if not poll_result.is_complete:
            poll_payload.update(self._build_export_gap_payload(pending_segments))
        self._emit("task_polled", poll_payload)
        if poll_result.is_complete:
            download_name = poll_result.download_name or f"{task.task_id}.bin"
            return TaskPollOutcome(
                ready_download=ReadyDownload(
                    start_ts=task.start_ts,
                    end_ts=task.end_ts,
                    task_id=task.task_id,
                    download_name=download_name,
                )
            )

        now = self.time_fn()
        if now + self.poll_interval > task.deadline_at:
            timeout_error = TimeoutError(f"task {task.task_id} did not finish before timeout")
            if task.timeout_retries < TASK_TIMEOUT_RETRY_LIMIT:
                retry_attempt = task.timeout_retries + 1
                self._emit(
                    "retrying_task_timeout",
                    {
                        "start_ts": task.start_ts,
                        "end_ts": task.end_ts,
                        "task_id": task.task_id,
                        "retry_attempt": retry_attempt,
                        "max_retries": TASK_TIMEOUT_RETRY_LIMIT,
                        "message": str(timeout_error),
                    },
                )
                return TaskPollOutcome(
                    retry_segment=PendingSegment(
                        task.start_ts,
                        task.end_ts,
                        timeout_retries=retry_attempt,
                    )
                )
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
            return TaskPollOutcome(task_failed=True)
        task.next_poll_at = now + self.poll_interval
        return TaskPollOutcome()

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
        return self._get_next_submit_at() <= now

    def _get_next_submit_at(self) -> float:
        next_submit_at = 0.0
        if self._last_export_request_at is not None:
            next_submit_at = self._last_export_request_at + EXPORT_GAP_SECONDS
        if self._export_cooldown_until is not None:
            next_submit_at = max(next_submit_at, self._export_cooldown_until)
        return next_submit_at

    def _build_export_gap_payload(
        self,
        pending_segments: deque[PendingSegment],
        *,
        now: float | None = None,
    ) -> dict[str, int]:
        remaining_seconds = self._get_export_gap_remaining_seconds(pending_segments, now=now)
        if remaining_seconds is None:
            return {}
        return {
            "export_gap_total_seconds": int(EXPORT_GAP_SECONDS),
            "export_gap_remaining_seconds": remaining_seconds,
        }

    def _get_export_gap_remaining_seconds(
        self,
        pending_segments: deque[PendingSegment],
        *,
        now: float | None = None,
    ) -> int | None:
        if not pending_segments or self._last_export_request_at is None:
            return self._get_cooldown_remaining_seconds(pending_segments, now=now)
        current_now = self.time_fn() if now is None else now
        remaining_seconds = int(math.ceil(self._get_next_submit_at() - current_now))
        if remaining_seconds <= 0:
            return None
        return remaining_seconds

    def _get_cooldown_remaining_seconds(
        self,
        pending_segments: deque[PendingSegment],
        *,
        now: float | None = None,
    ) -> int | None:
        if not pending_segments or self._export_cooldown_until is None:
            return None
        current_now = self.time_fn() if now is None else now
        remaining_seconds = int(math.ceil(self._export_cooldown_until - current_now))
        if remaining_seconds <= 0:
            return None
        return remaining_seconds

    def _sleep_until_next_action(
        self,
        pending_segments: deque[PendingSegment],
        active_tasks: dict[str, ActiveExportTask],
    ) -> None:
        now = self.time_fn()
        next_submit_at = math.inf
        if pending_segments:
            next_submit_at = self._get_next_submit_at()

        next_poll_at = min((task.next_poll_at for task in active_tasks.values()), default=math.inf)
        next_action_at = min(next_submit_at, next_poll_at)
        if math.isinf(next_action_at):
            return

        sleep_seconds = max(0.0, next_action_at - now)
        if sleep_seconds > 0:
            # Only emit waiting_export_gap when the next action is submitting a new export
            # (i.e., we are blocked by the export gap, not just waiting for a poll).
            if pending_segments and next_action_at == next_submit_at:
                remaining_seconds = self._get_export_gap_remaining_seconds(pending_segments, now=now)
                if remaining_seconds is not None:
                    self._emit(
                        "waiting_export_gap",
                        {
                            "total_seconds": int(EXPORT_GAP_SECONDS),
                            "remaining_seconds": remaining_seconds,
                        },
                    )
            self.sleep_fn(sleep_seconds)

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback(event_name, payload)
