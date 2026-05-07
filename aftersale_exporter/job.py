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
            "remediation": {
                "attempted": False,
                "resolved_dates": [],
                "unresolved_dates": [],
                "resolved_count": 0,
                "unresolved_count": 0,
            },
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
        elif event_name == "remediation":
            state = payload.get("state")
            if state == "completed":
                self.summary["remediation"] = {
                    "attempted": True,
                    "resolved_dates": payload.get("resolved_dates", []),
                    "unresolved_dates": payload.get("unresolved_dates", []),
                    "resolved_count": payload.get("resolved_count", 0),
                    "unresolved_count": payload.get("unresolved_count", 0),
                }
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
                mismatched_dates = self._print_merge_comparison(
                    tracker.daily_counts, merge_summary
                )
                if mismatched_dates:
                    self._remediate_mismatches(
                        tracker=tracker,
                        coordinator=coordinator,
                        merge_summary=merge_summary,
                        mismatched_dates=mismatched_dates,
                        daily_counts=tracker.daily_counts,
                    )

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
    ) -> list[str]:
        manifest_by_date = {item["date"]: item for item in daily_counts}
        all_dates = sorted(set(manifest_by_date) | set(merge_summary.daily_counts))
        matched_days = 0
        mismatched_days = 0
        skipped_days = 0
        mismatched_dates: list[str] = []

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
            if manifest_item is not None and manifest_item.get("status") == "counted":
                mismatched_dates.append(current_date)

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
        return mismatched_dates

    def _remediate_mismatches(
        self,
        *,
        tracker: ManifestTracker,
        coordinator: ExportCoordinator,
        merge_summary: MergeSummary,
        mismatched_dates: list[str],
        daily_counts: list[dict[str, Any]],
    ) -> None:
        daily_counts_by_date = {item["date"]: item for item in daily_counts}
        resolved_dates: list[str] = []
        unresolved_dates: list[str] = []
        merged_path = self.out_dir / "merged.xlsx"

        print(f"\n===== 开始修复 {len(mismatched_dates)} 天 mismatch =====")

        for date_str in mismatched_dates:
            daily_count = daily_counts_by_date.get(date_str)
            if daily_count is None or daily_count.get("status") != "counted":
                print(f"{date_str} | SKIP (无有效 API 查询数量)")
                unresolved_dates.append(date_str)
                continue

            day_start_ts = daily_count["start_ts"]
            day_end_ts = daily_count["end_ts"]
            manifest_total = int(daily_count["total"])
            merged_before = merge_summary.daily_counts.get(date_str, 0)

            print(
                f"\n[remediation] {date_str} | manifest={manifest_total} "
                f"| merged={merged_before} | 重新下载..."
            )

            self._emit_event(
                "remediation",
                {
                    "state": "downloading",
                    "date": date_str,
                    "start_ts": day_start_ts,
                    "end_ts": day_end_ts,
                },
            )

            try:
                day_result = coordinator.run(day_start_ts, day_end_ts)
            except Exception as exc:
                print(
                    f"[remediation] {date_str} | 下载失败: "
                    f"{exc.__class__.__name__}: {exc}"
                )
                unresolved_dates.append(date_str)
                continue

            if not day_result.segments:
                print(f"[remediation] {date_str} | 重下载无数据返回")
                unresolved_dates.append(date_str)
                continue

            input_files = [merged_path] + [
                seg.file_path for seg in day_result.segments
            ]
            try:
                merge_summary = merge_tabular_exports(
                    input_files,
                    merged_path,
                    timezone_name=self.timezone_name,
                )
            except Exception as exc:
                print(
                    f"[remediation] {date_str} | 合并失败: "
                    f"{exc.__class__.__name__}: {exc}"
                )
                unresolved_dates.append(date_str)
                continue

            new_merged_total = merge_summary.daily_counts.get(date_str, 0)

            if manifest_total == new_merged_total:
                resolved_dates.append(date_str)
                self._emit_event(
                    "remediation",
                    {
                        "state": "resolved",
                        "date": date_str,
                        "manifest_total": manifest_total,
                        "merged_total": new_merged_total,
                    },
                )
                print(
                    f"[remediation] {date_str} | manifest={manifest_total} "
                    f"| merged={new_merged_total} | RESOLVED"
                )
            else:
                unresolved_dates.append(date_str)
                self._emit_event(
                    "remediation",
                    {
                        "state": "unresolved",
                        "date": date_str,
                        "manifest_total": manifest_total,
                        "merged_total": new_merged_total,
                    },
                )
                print(
                    f"[remediation] {date_str} | manifest={manifest_total} "
                    f"| merged={new_merged_total} | UNRESOLVED"
                )

        print(
            f"\n修复汇总 | 共 {len(mismatched_dates)} 天 mismatch "
            f"| resolved={len(resolved_dates)} "
            f"| unresolved={len(unresolved_dates)}"
        )
        if unresolved_dates:
            print(f"  [UNRESOLVED] {', '.join(unresolved_dates)}")

        self._emit_event(
            "remediation",
            {
                "state": "completed",
                "resolved_dates": resolved_dates,
                "unresolved_dates": unresolved_dates,
                "resolved_count": len(resolved_dates),
                "unresolved_count": len(unresolved_dates),
            },
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
