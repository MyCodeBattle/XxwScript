# Repository Guidelines

## Project Structure & Module Organization
This package is the core of the `aftersale-exporter` project. Main modules live in `aftersale_exporter/`: `cli.py` parses arguments, `api.py` wraps HTTP calls, `workflow.py` manages split-and-poll scheduling, `job.py` writes `manifest.json` and `merged.xlsx`, and `merge.py` combines tabular exports. Runtime artifacts are written under `raw/` plus local manifest/output files. The full repo root is one level up and contains `pyproject.toml`, the wrapper script `cli.py`, `seed.curl`, and `tests/`.

## Build, Test, and Development Commands
Run commands from the git root after activating `conda activate zjh`:

- `python -m pip install -e .[dev]` installs the package and pytest extras.
- `python cli.py --help` checks the top-level CLI wrapper.
- `python -m aftersale_exporter --start "2026-04-29" --end "2026-04-30" --out-dir out` runs an export job.
- `python -m pytest` runs the full test suite in `tests/`.
- `python -m pytest tests/test_workflow.py` targets scheduler behavior only.

## Coding Style & Naming Conventions
Target Python 3.11+ and keep 4-space indentation. Follow the existing style: `snake_case` for modules, functions, and variables; `PascalCase` for classes; `UPPER_SNAKE_CASE` for constants. Preserve type hints, dataclasses, and small focused functions. No formatter or linter is configured today, so keep imports tidy and match surrounding code before introducing stylistic changes.

## Testing Guidelines
Tests are executed with pytest but mostly written with `unittest.TestCase`. Add new coverage under `tests/test_<module>.py`, mirroring the production module name. Prefer fake services, fake clocks, and temporary directories over live network calls. Changes to polling, splitting, manifest writing, or filename parsing should include regression tests.

## Commit & Pull Request Guidelines
The visible history uses Conventional Commit prefixes such as `chore:`; continue with `feat:`, `fix:`, `test:`, and `refactor:` plus a short imperative summary. Pull requests should explain the export scenario changed, list affected modules, and include representative CLI output or manifest examples when behavior changes.

## Security & Configuration Tips
Treat `seed.curl`, cookies, raw exports, and generated spreadsheets as sensitive merchant data. Keep local execution inside the `zjh` conda environment, and do not hardcode credentials or commit local export artifacts unless they are sanitized fixtures for tests.
