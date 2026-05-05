from __future__ import annotations

import sys
from typing import Any, TextIO

BAR_WIDTH = 24


def format_progress_event(event_name: str, payload: dict[str, Any]) -> str | None:
    if event_name == "split":
        return (
            f"[split] {payload['start_ts']}..{payload['end_ts']} "
            f"over limit, split at {payload['midpoint']}"
        )
    if event_name == "submitted":
        return f"[submitted] {payload['start_ts']}..{payload['end_ts']} task={payload['task_id']}"
    if event_name == "downloaded":
        return f"[downloaded] {payload['start_ts']}..{payload['end_ts']} -> {payload['file_path']}"
    if event_name == "failed":
        return (
            f"[failed] {payload['start_ts']}..{payload['end_ts']} "
            f"{payload['error_type']}: {payload['message']}"
        )
    return None


class TimeProgressBar:
    def __init__(
        self,
        *,
        start_ts: int,
        end_ts: int,
        stream: TextIO | None = None,
        bar_width: int = BAR_WIDTH,
    ) -> None:
        self.start_ts = start_ts
        self.end_ts = end_ts
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
            return f"splitting {payload['start_ts']}..{payload['end_ts']}"
        if event_name == "submitted":
            return f"task {payload['task_id']}"
        if event_name == "downloaded":
            return f"downloaded {payload['start_ts']}..{payload['end_ts']}"
        if event_name == "failed":
            return f"failed {payload['start_ts']}..{payload['end_ts']}"
        return event_name

    def _write_event_log(self, event_name: str, payload: dict[str, Any]) -> None:
        message = format_progress_event(event_name, payload)
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
        line = (
            f"[{bar}] {percent:5.1f}% "
            f"{self.completed_seconds}/{self.total_seconds}s {self.status}"
        )
        padded = line.ljust(self._last_line_length)
        self.stream.write(f"\r{padded}")
        self._last_line_length = len(line)
        self._flush()

    def _flush(self) -> None:
        flush = getattr(self.stream, "flush", None)
        if callable(flush):
            flush()
