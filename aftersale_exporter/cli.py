from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aftersale_exporter.api import AftersaleApiService
from aftersale_exporter.curl_template import DEFAULT_EXPORT_FILTER_CONFIG, parse_seed_curl
from aftersale_exporter.job import AftersaleExportJob
from aftersale_exporter.progress import TimeProgressBar

DATE_ONLY_FORMAT = "%Y-%m-%d"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED_CURL_PATH = PROJECT_ROOT / "seed.curl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export aftersale orders by binary-splitting time ranges.")
    parser.add_argument(
        "--start",
        required=True,
        help="Start time in local timezone, format: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="End time in local timezone, format: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS",
    )
    parser.add_argument(
        "--seed-curl",
        default=str(DEFAULT_SEED_CURL_PATH),
        help=f"Path to seed curl file copied from fxg.jinritemai.com (default: {DEFAULT_SEED_CURL_PATH})",
    )
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between task polling requests")
    parser.add_argument("--task-timeout", type=float, default=1800.0, help="Maximum seconds to wait for a single export task")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="IANA timezone for --start/--end parsing")
    return parser


def parse_local_timestamp(value: str, timezone_name: str, *, is_end: bool = False) -> int:
    try:
        local_time = datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        try:
            local_time = datetime.strptime(value, DATE_ONLY_FORMAT)
        except ValueError as exc:
            raise ValueError(
                f"invalid time value {value!r}, expected YYYY-MM-DD or YYYY-MM-DD HH:MM:SS"
            ) from exc
        if is_end:
            local_time = local_time.replace(hour=23, minute=59, second=59)
    return int(local_time.replace(tzinfo=ZoneInfo(timezone_name)).timestamp())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        start_ts = parse_local_timestamp(args.start, args.timezone, is_end=False)
        end_ts = parse_local_timestamp(args.end, args.timezone, is_end=True)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if start_ts > end_ts:
        raise SystemExit("start time must be earlier than end time")

    try:
        session_seed = parse_seed_curl(Path(args.seed_curl).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    service = AftersaleApiService(
        session_seed=session_seed,
        filter_config=DEFAULT_EXPORT_FILTER_CONFIG,
    )
    progress = TimeProgressBar(
        start_ts=start_ts,
        end_ts=end_ts,
        timezone_name=args.timezone,
    )
    job = AftersaleExportJob(
        service=service,
        out_dir=Path(args.out_dir),
        poll_interval=args.poll_interval,
        task_timeout=args.task_timeout,
        progress_callback=progress.handle_event,
    )
    try:
        job.run(
            start_ts=start_ts,
            end_ts=end_ts,
        )
    except Exception:
        progress.finish(success=False)
        raise
    progress.finish(success=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
