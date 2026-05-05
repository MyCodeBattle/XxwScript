from __future__ import annotations

from datetime import datetime
import sys
from typing import Any, TextIO
from zoneinfo import ZoneInfo

BAR_WIDTH = 24
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_local_timestamp(ts: int, timezone_name: str) -> str:
    return datetime.fromtimestamp(ts, tz=ZoneInfo(timezone_name)).strftime(TIMESTAMP_FORMAT)


def format_time_range(start_ts: int, end_ts: int, timezone_name: str) -> str:
    return (
        f"{format_local_timestamp(start_ts, timezone_name)}"
        f"..{format_local_timestamp(end_ts, timezone_name)}"
    )


def format_progress_event(
    event_name: str,
    payload: dict[str, Any],
    *,
    timezone_name: str,
) -> str | None:
    if event_name == "split":
        return (
            f"[split] {format_time_range(payload['start_ts'], payload['end_ts'], timezone_name)} "
            f"over limit, split at {format_local_timestamp(payload['midpoint'], timezone_name)}"
        )
    if event_name == "submitted":
        return (
            f"[submitted] "
            f"{format_time_range(payload['start_ts'], payload['end_ts'], timezone_name)} "
            f"task={payload['task_id']}"
        )
    if event_name == "downloaded":
        return (
            f"[downloaded] "
            f"{format_time_range(payload['start_ts'], payload['end_ts'], timezone_name)} "
            f"-> {payload['file_path']}"
        )
    if event_name == "failed":
        return (
            f"[failed] {format_time_range(payload['start_ts'], payload['end_ts'], timezone_name)} "
            f"{payload['error_type']}: {payload['message']}"
        )
    return None


class TimeProgressBar:
    def __init__(
        self,
        *,
        start_ts: int,
        end_ts: int,
        timezone_name: str = "Asia/Shanghai",
        stream: TextIO | None = None,
        bar_width: int = BAR_WIDTH,
    ) -> None:
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.timezone_name = timezone_name
        self.stream = sys.stdout if stream is None else stream
        self.bar_width = bar_width
        self.total_seconds = end_ts - start_ts + 1
        self.completed_seconds = 0
        self.completed_ranges: set[tuple[int, int]] = set()
        self.status = "waiting"
        self._last_line_length = 0
        self._render()

    def handle_event(self, event_name: str, payload: dict[str, Any]) -> None:
        self.status = self._build_status(event_name, payload)
        if event_name == "downloaded":
            self._mark_completed(payload["start_ts"], payload["end_ts"])
        self._write_event_log(event_name, payload)
        self._render()

    def finish(self, *, success: bool) -> None:
        if success:
            self.completed_seconds = self.total_seconds
            self.status = "completed"
        elif self.status == "waiting":
            self.status = "failed"
        self._render()
        self.stream.write("\n")
        self._flush()

    def _mark_completed(self, start_ts: int, end_ts: int) -> None:
        key = (start_ts, end_ts)
        if key in self.completed_ranges:
            return
        self.completed_ranges.add(key)
        self.completed_seconds += end_ts - start_ts + 1
        if self.completed_seconds > self.total_seconds:
            self.completed_seconds = self.total_seconds

    def _build_status(self, event_name: str, payload: dict[str, Any]) -> str:
        if event_name == "split":
            return (
                "splitting "
                f"{format_time_range(payload['start_ts'], payload['end_ts'], self.timezone_name)}"
            )
        if event_name == "submitted":
            return f"task {payload['task_id']}"
        if event_name == "waiting_task":
            return "等待文件生成"
        if event_name == "task_polled":
            return (
                "等待文件生成 | 最近请求 "
                f"{format_local_timestamp(payload['requested_at_ts'], self.timezone_name)} | "
                f"结果：{payload['result_text']}"
            )
        if event_name == "waiting_export_gap":
            return f"等待导出请求间隔 {payload['remaining_seconds']}s"
        if event_name == "downloaded":
            return (
                "downloaded "
                f"{format_time_range(payload['start_ts'], payload['end_ts'], self.timezone_name)}"
            )
        if event_name == "failed":
            return (
                "failed "
                f"{format_time_range(payload['start_ts'], payload['end_ts'], self.timezone_name)}"
            )
        return event_name

    def _write_event_log(self, event_name: str, payload: dict[str, Any]) -> None:
        message = format_progress_event(event_name, payload, timezone_name=self.timezone_name)
        if message is None:
            return
        self._clear_line()
        self.stream.write(f"{message}\n")
        self._flush()

    def _clear_line(self) -> None:
        if self._last_line_length == 0:
            self.stream.write("\r")
            return
        self.stream.write(f"\r{' ' * self._last_line_length}\r")

    def _render(self) -> None:
        ratio = self.completed_seconds / self.total_seconds
        percent = ratio * 100
        filled = min(self.bar_width, int(ratio * self.bar_width))
        bar = "#" * filled + "-" * (self.bar_width - filled)
        line = f"[{bar}] {percent:5.1f}% {self.status}"
        padded = line.ljust(self._last_line_length)
        self.stream.write(f"\r{padded}")
        self._last_line_length = len(line)
        self._flush()

    def _flush(self) -> None:
        flush = getattr(self.stream, "flush", None)
        if callable(flush):
            flush()
