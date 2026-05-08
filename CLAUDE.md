# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
python -m pytest

# Run a single test file
python -m pytest tests/test_workflow.py

# Run a specific test case or method
python -m pytest tests/test_api.py::TestDownloadFilenameResolution

# Run the exporter
python -m aftersale_exporter --start "2026-04-29" --end "2026-04-30" --out-dir ./out

# Install dev dependencies (pytest etc.)
python -m pip install -r requirement.txt
```

## Architecture

This is a **Douyin shop (fxg.jinritemai.com) aftersale order exporter**. It mimics the merchant backend's export feature by splitting a large time range into smaller chunks that stay under the platform's row limit, then merges the downloaded files into a single `merged.xlsx`.

### Core flow

```
CLI args → parse seed.curl → AftersaleApiService (HTTP)
         → ExportCoordinator (binary-split scheduler with poll/download loop)
         → merge_tabular_exports → merged.xlsx (+ manifest.json)
```

### Key modules

- **`cli.py`**: Argument parsing, timestamp conversion to Unix epoch, wiring components together.
- **`curl_template.py`**: Parses a browser-copied `curl` command into a `SessionSeed` (auth headers, cookies, query params). Whitelists specific query keys for the platform's security model.
- **`api.py`**: `AftersaleApiService` wraps all HTTP calls — create export, poll status, download file, count aftersales. Throws typed errors: `AuthenticationError`, `OverLimitError`, `ExportCooldownError`, `RetryableError`.
- **`workflow.py`**: `ExportCoordinator` is the core scheduler. Maintains three queues: `pending_segments`, `active_tasks`, `ready_downloads`. When `create_export` hits the `OverLimitError` (5万条 limit), it **binary-splits** the time range and re-queues. Enforces a 181s cooldown between successful export creations.
- **`job.py`**: `AftersaleExportJob` orchestrates the coordinator + merge + daily count reconciliation. `ManifestTracker` writes `manifest.json` incrementally for crash-recovery. Includes remediation logic that re-downloads individual days when the merged row count doesn't match the API count.
- **`merge.py`**: Reads `.xlsx`/`.csv` files, deduplicates by 售后单号, produces `merged.xlsx` with a `MergeSummary`.
- **`progress.py`**: `TimeProgressBar` renders a terminal progress bar with live updates in TTY mode, plain-text event logs in non-TTY mode.

### Testing conventions

Tests use `unittest.TestCase` and run via pytest. Test files mirror production modules under `tests/test_<module>.py`. Tests use fake services, fake clocks, and temp directories — no live network calls.

Error types are defined in `workflow.py` (not `api.py`) because both modules reference them, avoiding circular imports.

## Security

- `seed.curl` contains login cookies and tokens. Never commit it.
- `raw/` and `*.xlsx` files contain merchant data. `.gitignore` already excludes them.
