# Daily Count Mismatch Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After merging, compare daily record counts between downloaded files and API queries. For mismatched days, re-download and re-merge; report unresolved errors in terminal.

**Architecture:** Modify `_print_merge_comparison` to return mismatched dates. Add `_remediate_mismatches` method that iterates mismatched days, calls `ExportCoordinator.run()` for each, then `merge_tabular_exports()` with existing merged.xlsx plus new files. Wire into `run()`. Add remediation event formatting in progress.py.

**Tech Stack:** Python 3, openpyxl, unittest

---

### Task 1: Modify `_print_merge_comparison` to return mismatched dates

**Files:**
- Modify: `aftersale_exporter/job.py:281-321`

- [ ] **Step 1: Change return type and collect mismatched dates**

In `_print_merge_comparison`, add `mismatched_dates: list[str] = []` initialization, collect mismatched dates in the MISMATCH branch, and return the list.

Old code at line 281:
```python
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
```

Replace with:
```python
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
```

Old code at lines 305-307:
```python
            mismatched_days += 1
            print(f"{current_date} | manifest={manifest_total} | merged={merged_total} | MISMATCH")
```

Replace with:
```python
            mismatched_days += 1
            print(f"{current_date} | manifest={manifest_total} | merged={merged_total} | MISMATCH")
            if manifest_item is not None and manifest_item.get("status") == "counted":
                mismatched_dates.append(current_date)
```

Old code at line 320 (end of method, after the last print):
```python
        )
```

Replace with:
```python
        )
        return mismatched_dates
```

- [ ] **Step 2: Run existing tests to verify no regression**

Run: `python -m pytest tests/test_job.py -v`
Expected: All 7 existing tests PASS

---

### Task 2: Add remediation tracking to ManifestTracker

**Files:**
- Modify: `aftersale_exporter/job.py:24-34` (__init__)
- Modify: `aftersale_exporter/job.py:92-93` (handle, after count_failed branch)

- [ ] **Step 1: Add remediation key to summary dict in __init__**

After line 29 (`self.summary["daily_count_failed_days"] = 0`), add:
```python
        self.summary["remediation"] = {
            "attempted": False,
            "resolved_dates": [],
            "unresolved_dates": [],
            "resolved_count": 0,
            "unresolved_count": 0,
        }
```

- [ ] **Step 2: Add remediation event handler in handle()**

After line 92 (`daily_count.pop("total", None)`), add:
```python
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
```

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `python -m pytest tests/test_job.py -v`
Expected: All 7 existing tests PASS

---

### Task 3: Add `_remediate_mismatches` method

**Files:**
- Modify: `aftersale_exporter/job.py` (new method in `AftersaleExportJob` class, after `_print_merge_comparison`)

- [ ] **Step 1: Add the method**

Insert after `_print_merge_comparison` method (after line 321):

```python
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
```

- [ ] **Step 2: Run existing tests to verify no regression**

Run: `python -m pytest tests/test_job.py -v`
Expected: All 7 existing tests PASS

---

### Task 4: Wire remediation into `run()`

**Files:**
- Modify: `aftersale_exporter/job.py:236-237`

- [ ] **Step 1: Update run() to capture mismatched_dates and call remediation**

Old code:
```python
            if merge_summary is not None:
                self._print_merge_comparison(tracker.daily_counts, merge_summary)
```

Replace with:
```python
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
```

- [ ] **Step 2: Run existing tests to verify no regression**

Run: `python -m pytest tests/test_job.py -v`
Expected: All 7 existing tests PASS

---

### Task 5: Add remediation event formatting in progress.py

**Files:**
- Modify: `aftersale_exporter/progress.py:70-71` (format_progress_event, after count_failed)
- Modify: `aftersale_exporter/progress.py:239` (_write_plain_output event set)

- [ ] **Step 1: Add remediation formatting in format_progress_event**

After line 70 (`return None` after `count_failed` block), add:
```python
    if event_name == "remediation":
        state = payload.get("state")
        date = payload.get("date", "")
        if state == "downloading":
            return (
                f"[remediation] {date} re-downloading "
                f"{format_time_range(payload['start_ts'], payload['end_ts'], timezone_name)}"
            )
        if state == "resolved":
            return (
                f"[remediation] {date} "
                f"manifest={payload['manifest_total']} "
                f"merged={payload['merged_total']} | RESOLVED"
            )
        if state == "unresolved":
            return (
                f"[remediation] {date} "
                f"manifest={payload['manifest_total']} "
                f"merged={payload['merged_total']} | UNRESOLVED"
            )
        return None
```

- [ ] **Step 2: Add "remediation" to _write_plain_output event set**

Old code at line 239:
```python
        if event_name in {
            "split",
            "downloaded",
            "failed",
            "waiting_retry_cooldown",
            "retrying_task_timeout",
            "counted",
            "count_failed",
        }:
```

Replace with:
```python
        if event_name in {
            "split",
            "downloaded",
            "failed",
            "waiting_retry_cooldown",
            "retrying_task_timeout",
            "counted",
            "count_failed",
            "remediation",
        }:
```

- [ ] **Step 3: Run tests to verify no regression**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

---

### Task 6: Add tests for remediation behavior

**Files:**
- Modify: `tests/test_job.py` (add new test methods and a helper fake service)

- [ ] **Step 1: Add RemediationFakeService helper class**

Add after `DailyCountFakeService` class (after line 204):

```python
class RemediationFakeService:
    """Fake service that returns different export data on second call for same range."""

    def __init__(
        self,
        *,
        count_totals: dict[tuple[int, int], int],
        first_export_rows: dict[str, list[tuple[str, str, str]]],
        second_export_rows: dict[str, list[tuple[str, str, str]]] | None = None,
    ) -> None:
        self.count_totals = count_totals
        self.count_requests: list[tuple[int, int]] = []
        self.submissions: list[tuple[int, int]] = []
        self.first_export_rows = first_export_rows
        self.second_export_rows = second_export_rows or {}
        self._export_call_counts: dict[tuple[int, int], int] = {}

    def create_export(self, start_ts: int, end_ts: int) -> str:
        self.submissions.append((start_ts, end_ts))
        return f"task-{start_ts}-{end_ts}"

    def wait_for_task(self, task_id, poll_interval, timeout, status_callback=None):
        raise AssertionError("job workflow should poll tasks incrementally")

    def poll_task(self, task_id: str) -> TaskPollResult:
        return TaskPollResult(
            requested_at_ts=1234567890,
            result_text="文件已生成",
            is_complete=True,
            download_name=f"{task_id}.csv",
        )

    def download_export(self, task_id: str, destination: Path) -> Path:
        key = tuple(int(x) for x in task_id.replace("task-", "").split("-"))
        call_index = self._export_call_counts.get(key, 0)
        self._export_call_counts[key] = call_index + 1

        rows_map = self.first_export_rows if call_index == 0 else self.second_export_rows
        rows = rows_map.get(task_id)
        if rows is None:
            rows = next(iter(rows_map.values()))
        lines = ["售后单号,售后完结时间,value"]
        for order_no, finished_at, value in rows:
            lines.append(f"{order_no},{finished_at},{value}")
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return destination

    def count_aftersales(self, start_ts: int, end_ts: int) -> int:
        key = (start_ts, end_ts)
        self.count_requests.append(key)
        return self.count_totals[key]
```

- [ ] **Step 2: Test — single day mismatch resolved after remediation**

Add test method to `AftersaleExportJobTests`:

```python
    def test_remediation_resolves_single_day_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            start_ts = local_ts("2026-05-01 00:00:00")
            end_ts = local_ts("2026-05-01 23:59:59")
            service = RemediationFakeService(
                count_totals={(start_ts, end_ts): 3},
                first_export_rows={
                    f"task-{start_ts}-{end_ts}": [
                        ("A1", "2026-05-01 09:00:00", "v1"),
                    ]
                },
                second_export_rows={
                    f"task-{start_ts}-{end_ts}": [
                        ("A2", "2026-05-01 10:00:00", "v2"),
                        ("A3", "2026-05-01 11:00:00", "v3"),
                    ]
                },
            )
            job = AftersaleExportJob(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
                timezone_name="Asia/Shanghai",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                job.run(start_ts=start_ts, end_ts=end_ts)

            output = stdout.getvalue()
            self.assertIn(
                "2026-05-01 | manifest=3 | merged=1 | MISMATCH", output
            )
            self.assertIn("开始修复 1 天 mismatch", output)
            self.assertIn("RESOLVED", output)
            self.assertIn("修复汇总 | 共 1 天 mismatch | resolved=1 | unresolved=0", output)
            self.assertNotIn("UNRESOLVED", output)

            manifest = json.loads(
                (Path(tmpdir) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["summary"]["remediation"]["attempted"])
            self.assertEqual(manifest["summary"]["remediation"]["resolved_count"], 1)
            self.assertEqual(manifest["summary"]["remediation"]["unresolved_count"], 0)
```

- [ ] **Step 3: Test — single day mismatch remains unresolved**

Add test method:

```python
    def test_remediation_unresolved_when_redownload_yields_same_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            start_ts = local_ts("2026-05-01 00:00:00")
            end_ts = local_ts("2026-05-01 23:59:59")
            rows = [("A1", "2026-05-01 09:00:00", "v1")]
            service = RemediationFakeService(
                count_totals={(start_ts, end_ts): 3},
                first_export_rows={f"task-{start_ts}-{end_ts}": rows},
                second_export_rows={f"task-{start_ts}-{end_ts}": rows},
            )
            job = AftersaleExportJob(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
                timezone_name="Asia/Shanghai",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                job.run(start_ts=start_ts, end_ts=end_ts)

            output = stdout.getvalue()
            self.assertIn("UNRESOLVED", output)
            self.assertIn("resolved=0 | unresolved=1", output)

            manifest = json.loads(
                (Path(tmpdir) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["summary"]["remediation"]["attempted"])
            self.assertEqual(manifest["summary"]["remediation"]["unresolved_count"], 1)
```

- [ ] **Step 4: Test — no remediation when all days match**

Add test method:

```python
    def test_no_remediation_when_all_days_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            start_ts = local_ts("2026-05-01 00:00:00")
            end_ts = local_ts("2026-05-01 23:59:59")
            service = DailyCountFakeService(
                count_totals={(start_ts, end_ts): 2},
                export_rows={
                    f"task-{start_ts}-{end_ts}": [
                        ("A1", "2026-05-01 09:00:00", "v1"),
                        ("A2", "2026-05-01 10:00:00", "v2"),
                    ]
                },
            )
            job = AftersaleExportJob(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
                timezone_name="Asia/Shanghai",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                job.run(start_ts=start_ts, end_ts=end_ts)

            output = stdout.getvalue()
            self.assertIn("MATCH", output)
            self.assertNotIn("开始修复", output)

            manifest = json.loads(
                (Path(tmpdir) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["summary"]["remediation"]["attempted"])
```

- [ ] **Step 5: Test — skipped days (count_failed) not included in remediation**

Add test method:

```python
    def test_skipped_days_not_in_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = FakeClock()
            first_start = local_ts("2026-05-01 08:00:00")
            first_end = local_ts("2026-05-01 23:59:59")
            second_start = local_ts("2026-05-02 00:00:00")
            second_end = local_ts("2026-05-02 08:00:00")
            service = DailyCountFakeService(
                count_totals={(first_start, first_end): 5},
                count_failures={(second_start, second_end): RuntimeError("boom")},
                export_rows={
                    f"task-{first_start}-{second_end}": [
                        ("A1", "2026-05-01 12:00:00", "v1"),
                        ("A2", "2026-05-02 02:00:00", "v2"),
                    ]
                },
            )
            job = AftersaleExportJob(
                service=service,
                out_dir=Path(tmpdir),
                poll_interval=0.01,
                task_timeout=1.0,
                sleep_fn=clock.sleep,
                time_fn=clock.monotonic,
                timezone_name="Asia/Shanghai",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                job.run(start_ts=first_start, end_ts=second_end)

            output = stdout.getvalue()
            self.assertIn("SKIPPED", output)
            self.assertNotIn("开始修复", output)
```

- [ ] **Step 6: Run all tests**

Run: `python -m pytest tests/test_job.py -v`
Expected: All 11 tests PASS (7 existing + 4 new)

- [ ] **Step 7: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

---

### Task 7: Manual smoke test (optional, requires valid seed.curl)

- [ ] **Step 1: Run the CLI on a small time range**

Run: `python cli.py --start "2026-05-01 00:00:00" --end "2026-05-01 23:59:59" --seed-curl seed.curl --out-dir /tmp/test-remediation --poll-interval 5 --task-timeout 60`
Expected: Observe remediation output in terminal if any mismatches exist

---

### Task 8: Commit

- [ ] **Step 1: Stage and commit**

```bash
git add aftersale_exporter/job.py aftersale_exporter/progress.py tests/test_job.py
git commit -m "feat: add daily count mismatch remediation after merge

- _print_merge_comparison now returns mismatched date list
- _remediate_mismatches re-downloads mismatched days and re-merges
- ManifestTracker records remediation results
- progress.py formats remediation events for terminal output

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
