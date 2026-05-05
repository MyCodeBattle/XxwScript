from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook


def merge_tabular_exports(files: list[Path], destination: Path) -> Path:
    rows: list[dict[str, Any]] = []
    columns: list[str] = []

    for file_path in files:
        file_rows = _read_rows(file_path)
        for row in file_rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
            rows.append(row)

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(columns)
    for row in rows:
        sheet.append([row.get(column) for column in columns])
    workbook.save(destination)
    return destination


def _read_rows(file_path: Path) -> list[dict[str, Any]]:
    if file_path.suffix.lower() == ".csv":
        with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    if file_path.suffix.lower() == ".xlsx":
        workbook = load_workbook(file_path)
        sheet = workbook.active
        values = list(sheet.iter_rows(values_only=True))
        if not values:
            return []
        headers = [str(value) if value is not None else "" for value in values[0]]
        rows: list[dict[str, Any]] = []
        for value_row in values[1:]:
            row: dict[str, Any] = {}
            for idx, header in enumerate(headers):
                row[header] = value_row[idx] if idx < len(value_row) else None
            rows.append(row)
        return rows

    raise ValueError(f"Unsupported file type for merge: {file_path.suffix}")
