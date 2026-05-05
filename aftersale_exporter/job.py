from __future__ import annotations

from datetime import datetime, time as datetime_time, timedelta
import json
from pathlib import Path
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from aftersale_exporter.merge import MergeSummary, merge_tabular_exports
from aftersale_exporter.workflow import ExportCoordinator, ExportRunResult

MANIFEST_RUN_SEPARATOR_PREFIX = "===== manifest run "


class ManifestTracker:
    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path
        self._history_text = self._load_history_text()
        self._current_run_separator = (
            f"{MANIFEST_RUN_SEPARATOR_PREFIX}"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ====="
        )
        self.summary: dict[str, Any] = {
            "segment_count": 0,
            "failed_count": 0,
            "daily_count_days": 0,
            "daily_count_failed_days": 0,
        }
        self.segments: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.splits: list[dict[str, Any]] = []
        self.daily_counts: list[dict[str, Any]] = []
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
        elif event_name == "counted":
            daily_count = self._find_or_create_daily_count(
                payload["date"],
                payload["start_ts"],
                payload["end_ts"],
            )
            daily_count.update(
                {
                    "status": "counted",
                    "total": payload["total"],
                }
            )
            daily_count.pop("error_type", None)
            daily_count.pop("message", None)
        elif event_name == "count_failed":
            daily_count = self._find_or_create_daily_count(
                payload["date"],
                payload["start_ts"],
                payload["end_ts"],
            )
            daily_count.update(
                {
                    "status": "failed",
                    "error_type": payload["error_type"],
                    "message": payload["message"],
                }
            )
            daily_count.pop("total", None)
        self.summary["segment_count"] = len(self.segments)
        self.summary["daily_count_days"] = len(self.daily_counts)
        self.summary["daily_count_failed_days"] = sum(
            1 for item in self.daily_counts if item.get("status") == "failed"
        )
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
            "daily_counts": self.daily_counts,
        }
        manifest_text = json.dumps(payload, ensure_ascii=False, indent=2)
        if self._history_text:
            manifest_text = (
                f"{self._history_text}\n\n"
                f"{self._current_run_separator}\n"
                f"{manifest_text}"
            )
        self.manifest_path.write_text(manifest_text, encoding="utf-8")

    def _load_history_text(self) -> str:
        if not self.manifest_path.exists():
            return ""
        existing_text = self.manifest_path.read_text(encoding="utf-8").strip()
        if not existing_text:
            return ""
        return existing_text

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

    def _find_or_create_daily_count(
        self,
        date: str,
        start_ts: int,
        end_ts: int,
    ) -> dict[str, Any]:
        for daily_count in self.daily_counts:
            if daily_count["date"] == date:
                return daily_count
        daily_count = {
            "date": date,
            "start_ts": start_ts,
            "end_ts": end_ts,
        }
        self.daily_counts.append(daily_count)
        return daily_count


class AftersaleExportJob:
    def __init__(
        self,
        *,
        service,
        out_dir: Path,
        poll_interval: float,
        task_timeout: float,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        timezone_name: str = "Asia/Shanghai",
        sleep_fn: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self.service = service
        self.out_dir = out_dir
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.progress_callback = progress_callback
        self.timezone_name = timezone_name
        self.sleep_fn = time.sleep if sleep_fn is None else sleep_fn
        self.time_fn = time.monotonic if time_fn is None else time_fn
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
            sleep_fn=self.sleep_fn,
            time_fn=self.time_fn,
        )
        result: ExportRunResult | None = None
        merge_error: str | None = None
        merge_summary: MergeSummary | None = None

        try:
            try:
                result = coordinator.run(start_ts, end_ts)
            except Exception:
                tracker.finalize(result, merge_error=merge_error)
                raise

            try:
                merge_summary = merge_tabular_exports(
                    [segment.file_path for segment in result.segments],
                    self.out_dir / "merged.xlsx",
                    timezone_name=self.timezone_name,
                )
            except ValueError as exc:
                merge_error = str(exc)
            except Exception:
                tracker.finalize(result, merge_error=merge_error)
                raise

            try:
                self._record_daily_counts(start_ts, end_ts)
            except Exception:
                tracker.finalize(result, merge_error=merge_error)
                raise

            if merge_summary is not None:
                self._print_merge_comparison(tracker.daily_counts, merge_summary)

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

    def _record_daily_counts(self, start_ts: int, end_ts: int) -> None:
        for date, day_start_ts, day_end_ts in _iter_daily_ranges(
            start_ts,
            end_ts,
            timezone_name=self.timezone_name,
        ):
            try:
                total = self.service.count_aftersales(day_start_ts, day_end_ts)
            except Exception as exc:
                self._emit_event(
                    "count_failed",
                    {
                        "date": date,
                        "start_ts": day_start_ts,
                        "end_ts": day_end_ts,
                        "error_type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                )
                continue
            self._emit_event(
                "counted",
                {
                    "date": date,
                    "start_ts": day_start_ts,
                    "end_ts": day_end_ts,
                    "total": total,
                },
            )

    def _print_merge_comparison(
        self,
        daily_counts: list[dict[str, Any]],
        merge_summary: MergeSummary,
    ) -> None:
        manifest_by_date = {item["date"]: item for item in daily_counts}
        all_dates = sorted(set(manifest_by_date) | set(merge_summary.daily_counts))
        matched_days = 0
        mismatched_days = 0
        skipped_days = 0

        for current_date in all_dates:
            manifest_item = manifest_by_date.get(current_date)
            merged_total = merge_summary.daily_counts.get(current_date, 0)
            if manifest_item is not None and manifest_item.get("status") != "counted":
                skipped_days += 1
                print(f"{current_date} | manifest=FAILED | merged={merged_total} | SKIPPED")
                continue

            manifest_total = 0 if manifest_item is None else int(manifest_item["total"])
            if manifest_total == merged_total:
                matched_days += 1
                print(f"{current_date} | manifest={manifest_total} | merged={merged_total} | MATCH")
                continue

            mismatched_days += 1
            print(f"{current_date} | manifest={manifest_total} | merged={merged_total} | MISMATCH")

        print(
            "去重汇总 | "
            f"total={merge_summary.total_rows} | "
            f"unique={merge_summary.unique_rows} | "
            f"duplicates={merge_summary.duplicate_rows}"
        )
        print(
            "比对汇总 | "
            f"match={matched_days} | "
            f"mismatch={mismatched_days} | "
            f"skipped={skipped_days}"
        )


def _iter_daily_ranges(
    start_ts: int,
    end_ts: int,
    *,
    timezone_name: str,
) -> list[tuple[str, int, int]]:
    timezone = ZoneInfo(timezone_name)
    start_date = datetime.fromtimestamp(start_ts, tz=timezone).date()
    end_date = datetime.fromtimestamp(end_ts, tz=timezone).date()
    current_date = start_date
    ranges: list[tuple[str, int, int]] = []

    while current_date <= end_date:
        day_start = datetime.combine(current_date, datetime_time.min, tzinfo=timezone)
        next_day_start = datetime.combine(
            current_date + timedelta(days=1),
            datetime_time.min,
            tzinfo=timezone,
        )
        day_start_ts = max(start_ts, int(day_start.timestamp()))
        day_end_ts = min(end_ts, int(next_day_start.timestamp()) - 1)
        ranges.append((current_date.isoformat(), day_start_ts, day_end_ts))
        current_date += timedelta(days=1)

    return ranges
