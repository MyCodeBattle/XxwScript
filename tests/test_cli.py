from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import aftersale_exporter.cli as cli_module
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from aftersale_exporter.cli import build_parser, main, parse_local_timestamp
from aftersale_exporter.curl_template import DEFAULT_EXPORT_FILTER_CONFIG


SEED_CURL = r"""
curl 'https://fxg.jinritemai.com/ffa/maftersale/aftersale/list?appid=1&__token=abc&_bid=ffa_aftersale&aid=4272&aftersale_platform_source=fxg&msToken=seed-token&a_bogus=seed-bogus&verifyFp=verify-seed&fp=verify-seed&_lid=oldlid' \
  -H 'content-type: application/json;charset=UTF-8' \
  -b 'sessionid=abc123'
"""

BAD_SEED_CURL = r"""
curl 'https://fxg.jinritemai.com/ffa/maftersale/aftersale/list?appid=1&__token=abc&_bid=ffa_aftersale&aid=4272&aftersale_platform_source=fxg&verifyFp=verify-seed&fp=verify-seed&_lid=oldlid' \
  -H 'content-type: application/json;charset=UTF-8' \
  -b 'sessionid=abc123'
"""


class CliTests(unittest.TestCase):
    def test_task_timeout_defaults_to_ten_minutes(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.get_default("task_timeout"), 600.0)

    def test_cli_script_runs_directly_without_import_error(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        script_path = repo_root / "aftersale_exporter" / "cli.py"

        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Export aftersale orders", result.stdout)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_repo_root_cli_wrapper_runs(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        script_path = repo_root / "cli.py"

        result = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Export aftersale orders", result.stdout)

    def test_parse_local_timestamp_defaults_to_shanghai_timezone(self) -> None:
        actual = parse_local_timestamp("2026-04-29 00:00:00", "Asia/Shanghai")

        expected = int(
            datetime(2026, 4, 29, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        )
        self.assertEqual(actual, expected)

    def test_parse_local_timestamp_uses_start_of_day_for_date_only_start(self) -> None:
        actual = parse_local_timestamp("2026-04-29", "Asia/Shanghai", is_end=False)

        expected = int(
            datetime(2026, 4, 29, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        )
        self.assertEqual(actual, expected)

    def test_parse_local_timestamp_uses_end_of_day_for_date_only_end(self) -> None:
        actual = parse_local_timestamp("2026-04-29", "Asia/Shanghai", is_end=True)

        expected = int(
            datetime(2026, 4, 29, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        )
        self.assertEqual(actual, expected)

    def test_main_builds_service_from_seed_curl_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            seed_path = Path(tmpdir) / "seed.curl"
            out_dir = Path(tmpdir) / "out"
            seed_path.write_text(SEED_CURL, encoding="utf-8")

            mock_job = MagicMock()
            with patch("aftersale_exporter.cli.AftersaleApiService") as service_cls, patch(
                "aftersale_exporter.cli.AftersaleExportJob",
                return_value=mock_job,
            ) as job_cls:
                exit_code = main(
                    [
                        "--start",
                        "2026-04-29 00:00:00",
                        "--end",
                        "2026-04-29 00:00:05",
                        "--seed-curl",
                        str(seed_path),
                        "--out-dir",
                        str(out_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service_cls.call_count, 1)
        self.assertIn("session_seed", service_cls.call_args.kwargs)
        self.assertIn("filter_config", service_cls.call_args.kwargs)
        self.assertEqual(
            service_cls.call_args.kwargs["filter_config"],
            DEFAULT_EXPORT_FILTER_CONFIG,
        )
        self.assertEqual(job_cls.call_count, 1)
        self.assertIn("progress_callback", job_cls.call_args.kwargs)
        mock_job.run.assert_called_once_with(
            start_ts=parse_local_timestamp("2026-04-29 00:00:00", "Asia/Shanghai"),
            end_ts=parse_local_timestamp("2026-04-29 00:00:05", "Asia/Shanghai"),
        )

    def test_main_accepts_date_only_inputs_and_expands_full_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            seed_path = Path(tmpdir) / "seed.curl"
            out_dir = Path(tmpdir) / "out"
            seed_path.write_text(SEED_CURL, encoding="utf-8")

            mock_job = MagicMock()
            with patch("aftersale_exporter.cli.AftersaleApiService") as service_cls, patch(
                "aftersale_exporter.cli.AftersaleExportJob",
                return_value=mock_job,
            ) as job_cls:
                exit_code = main(
                    [
                        "--start",
                        "2026-04-29",
                        "--end",
                        "2026-04-30",
                        "--seed-curl",
                        str(seed_path),
                        "--out-dir",
                        str(out_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service_cls.call_count, 1)
        self.assertEqual(job_cls.call_count, 1)
        mock_job.run.assert_called_once_with(
            start_ts=parse_local_timestamp("2026-04-29", "Asia/Shanghai", is_end=False),
            end_ts=parse_local_timestamp("2026-04-30", "Asia/Shanghai", is_end=True),
        )

    def test_main_accepts_mixed_date_only_and_full_timestamp_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            seed_path = Path(tmpdir) / "seed.curl"
            out_dir = Path(tmpdir) / "out"
            seed_path.write_text(SEED_CURL, encoding="utf-8")

            mock_job = MagicMock()
            with patch("aftersale_exporter.cli.AftersaleApiService") as service_cls, patch(
                "aftersale_exporter.cli.AftersaleExportJob",
                return_value=mock_job,
            ) as job_cls:
                exit_code = main(
                    [
                        "--start",
                        "2026-04-29",
                        "--end",
                        "2026-04-30 12:00:00",
                        "--seed-curl",
                        str(seed_path),
                        "--out-dir",
                        str(out_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service_cls.call_count, 1)
        self.assertEqual(job_cls.call_count, 1)
        mock_job.run.assert_called_once_with(
            start_ts=parse_local_timestamp("2026-04-29", "Asia/Shanghai", is_end=False),
            end_ts=parse_local_timestamp("2026-04-30 12:00:00", "Asia/Shanghai"),
        )

    def test_main_uses_project_root_default_seed_curl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            seed_path = Path(tmpdir) / "seed.curl"
            out_dir = Path(tmpdir) / "out"
            seed_path.write_text(SEED_CURL, encoding="utf-8")

            mock_job = MagicMock()
            with patch.object(cli_module, "DEFAULT_SEED_CURL_PATH", seed_path, create=True), patch(
                "aftersale_exporter.cli.AftersaleApiService"
            ) as service_cls, patch(
                "aftersale_exporter.cli.AftersaleExportJob",
                return_value=mock_job,
            ) as job_cls:
                exit_code = main(
                    [
                        "--start",
                        "2026-04-29 00:00:00",
                        "--end",
                        "2026-04-29 00:00:05",
                        "--out-dir",
                        str(out_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(service_cls.call_count, 1)
        self.assertEqual(job_cls.call_count, 1)
        mock_job.run.assert_called_once_with(
            start_ts=parse_local_timestamp("2026-04-29 00:00:00", "Asia/Shanghai"),
            end_ts=parse_local_timestamp("2026-04-29 00:00:05", "Asia/Shanghai"),
        )

    def test_main_rejects_start_after_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            seed_path = Path(tmpdir) / "seed.curl"
            seed_path.write_text(SEED_CURL, encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "start time must be earlier than end time"):
                main(
                    [
                        "--start",
                        "2026-04-29 00:00:05",
                        "--end",
                        "2026-04-29 00:00:00",
                        "--seed-curl",
                        str(seed_path),
                        "--out-dir",
                        str(Path(tmpdir) / "out"),
                    ]
                )

    def test_main_rejects_seed_curl_missing_required_query_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            seed_path = Path(tmpdir) / "seed.curl"
            seed_path.write_text(BAD_SEED_CURL, encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "missing required query fields"):
                main(
                    [
                        "--start",
                        "2026-04-29 00:00:00",
                        "--end",
                        "2026-04-29 00:00:05",
                        "--seed-curl",
                        str(seed_path),
                        "--out-dir",
                        str(Path(tmpdir) / "out"),
                    ]
                )

    def test_main_uses_time_progress_bar_and_finishes_on_success(self) -> None:
        self.assertTrue(hasattr(cli_module, "TimeProgressBar"))
        if not hasattr(cli_module, "TimeProgressBar"):
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            seed_path = Path(tmpdir) / "seed.curl"
            out_dir = Path(tmpdir) / "out"
            seed_path.write_text(SEED_CURL, encoding="utf-8")

            mock_job = MagicMock()
            mock_progress = MagicMock()
            with patch("aftersale_exporter.cli.AftersaleApiService"), patch(
                "aftersale_exporter.cli.AftersaleExportJob",
                return_value=mock_job,
            ) as job_cls, patch(
                "aftersale_exporter.cli.TimeProgressBar",
                return_value=mock_progress,
            ) as progress_cls:
                exit_code = main(
                    [
                        "--start",
                        "2026-04-29 00:00:00",
                        "--end",
                        "2026-04-29 00:00:05",
                        "--seed-curl",
                        str(seed_path),
                        "--out-dir",
                        str(out_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)
        progress_cls.assert_called_once_with(
            start_ts=parse_local_timestamp("2026-04-29 00:00:00", "Asia/Shanghai"),
            end_ts=parse_local_timestamp("2026-04-29 00:00:05", "Asia/Shanghai"),
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(job_cls.call_args.kwargs["progress_callback"], mock_progress.handle_event)
        mock_progress.finish.assert_called_once_with(success=True)

    def test_main_finishes_progress_bar_on_failure(self) -> None:
        self.assertTrue(hasattr(cli_module, "TimeProgressBar"))
        if not hasattr(cli_module, "TimeProgressBar"):
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            seed_path = Path(tmpdir) / "seed.curl"
            out_dir = Path(tmpdir) / "out"
            seed_path.write_text(SEED_CURL, encoding="utf-8")

            mock_job = MagicMock()
            mock_job.run.side_effect = RuntimeError("boom")
            mock_progress = MagicMock()
            with patch("aftersale_exporter.cli.AftersaleApiService"), patch(
                "aftersale_exporter.cli.AftersaleExportJob",
                return_value=mock_job,
            ), patch(
                "aftersale_exporter.cli.TimeProgressBar",
                return_value=mock_progress,
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    main(
                        [
                            "--start",
                            "2026-04-29 00:00:00",
                            "--end",
                            "2026-04-29 00:00:05",
                            "--seed-curl",
                            str(seed_path),
                            "--out-dir",
                            str(out_dir),
                        ]
                    )

        mock_progress.finish.assert_called_once_with(success=False)


if __name__ == "__main__":
    unittest.main()
