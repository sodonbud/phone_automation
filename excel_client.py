"""Excel read/write client using openpyxl.

Keeps a single Workbook open for the run's lifetime; caller must call .close()
(or use as a context manager) to guarantee the final save.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from logger import get_logger

# Expected column names (order must match the sheet header row)
COLUMNS = [
    "Step",
    "Action",
    "Target Phone",
    "Number/Code",
    "Value",
    "Expected Result",
    "Actual Result",
    "Pass/Fail",
    "Notes",
    "Timestamp",
]

PASS_FILL = PatternFill("solid", fgColor="00B050")
FAIL_FILL = PatternFill("solid", fgColor="FF0000")
SKIP_FILL = PatternFill("solid", fgColor="FFFF00")
_RESULT_FILLS = {"PASS": PASS_FILL, "FAIL": FAIL_FILL, "SKIP": SKIP_FILL}

# Column indices (1-based) for result columns
_COL_ACTUAL = COLUMNS.index("Actual Result") + 1
_COL_PASS_FAIL = COLUMNS.index("Pass/Fail") + 1
_COL_NOTES = COLUMNS.index("Notes") + 1
_COL_TIMESTAMP = COLUMNS.index("Timestamp") + 1


class ExcelClient:
    def __init__(self, file_path: str, sheet_name: str) -> None:
        self._path = file_path
        self._sheet_name = sheet_name
        self._wb: openpyxl.Workbook | None = None
        self._ws = None
        self._header_map: dict[str, int] = {}  # column name → 1-based col index
        self.log = get_logger()

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "ExcelClient":
        self.open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        if not os.path.exists(self._path):
            raise FileNotFoundError(f"Excel file not found: {self._path}")
        try:
            self._wb = openpyxl.load_workbook(
                self._path, keep_vba=False, data_only=True
            )
        except Exception as exc:
            raise RuntimeError(f"Cannot open Excel file '{self._path}': {exc}") from exc

        if self._sheet_name not in self._wb.sheetnames:
            available = ", ".join(self._wb.sheetnames)
            raise ValueError(
                f"Sheet '{self._sheet_name}' not found. Available sheets: {available}"
            )

        self._ws = self._wb[self._sheet_name]
        self._build_header_map()
        self.log.info("Opened '%s' sheet '%s'", self._path, self._sheet_name)

    def close(self) -> None:
        if self._wb is not None:
            try:
                self._wb.save(self._path)
                self.log.info("Workbook saved to '%s'", self._path)
            except Exception as exc:
                self.log.error("Failed to save workbook: %s", exc)
            finally:
                self._wb = None
                self._ws = None

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _build_header_map(self) -> None:
        header_row = next(self._ws.iter_rows(min_row=1, max_row=1, values_only=True))
        self._header_map = {
            str(cell).strip(): idx + 1
            for idx, cell in enumerate(header_row)
            if cell is not None
        }

    def get_test_cases(self) -> list[dict[str, Any]]:
        """Return all data rows as a list of dicts keyed by header name."""
        rows = []
        for row_idx, row in enumerate(
            self._ws.iter_rows(min_row=2, values_only=True), start=2
        ):
            # Skip completely blank rows
            if all(cell is None or str(cell).strip() == "" for cell in row):
                continue
            record: dict[str, Any] = {"_row": row_idx}
            for name, col in self._header_map.items():
                val = row[col - 1] if col - 1 < len(row) else None
                record[name] = "" if val is None else str(val).strip()
            rows.append(record)
        self.log.info("Loaded %d test case(s)", len(rows))
        return rows

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def write_result(
        self,
        row_index: int,
        actual: str,
        result: str,
        notes: str,
        timestamp: str | None = None,
    ) -> None:
        """Update Actual Result, Pass/Fail, Notes, Timestamp for *row_index* (1-based)."""
        if self._ws is None:
            raise RuntimeError("Workbook is not open.")
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        result_upper = result.strip().upper()
        fill = _RESULT_FILLS.get(result_upper)

        def _set(col: int, value: Any, apply_fill: bool = False) -> None:
            cell = self._ws.cell(row=row_index, column=col)
            cell.value = value
            if apply_fill and fill:
                cell.fill = fill

        _set(_COL_ACTUAL, actual)
        _set(_COL_PASS_FAIL, result_upper, apply_fill=True)
        _set(_COL_NOTES, notes)
        _set(_COL_TIMESTAMP, timestamp)

        # Persist immediately so a crash doesn't lose earlier results
        try:
            self._wb.save(self._path)
        except Exception as exc:
            self.log.error("Failed to save after row %d: %s", row_index, exc)

    # ------------------------------------------------------------------
    # Template generation (used by generate_template.py / main.py)
    # ------------------------------------------------------------------

    @staticmethod
    def create_template(file_path: str, sheet_name: str = "TestCases") -> None:
        """Write a ready-to-use template workbook to *file_path*."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name

        # Header styling
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="4472C4")
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for col_idx, name in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=1, column=col_idx, value=name)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align

        ws.row_dimensions[1].height = 30

        # Column widths
        widths = [6, 12, 14, 20, 20, 30, 30, 10, 30, 20]
        for col_idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        # Example rows: one per action type
        examples = [
            # ── Call flow ──────────────────────────────────────────────────────────
            # Phone1 calls Phone2 → Phone2 answers → both record → end call
            ["1",  "CALL",         "Phone1", "+97699002222", "",                    "",                   "", "", "", ""],
            ["2",  "ANSWER_CALL",  "Phone2", "",             "30",                  "Call answered",       "", "", "", ""],
            ["3",  "START_RECORD", "Phone1", "",             "recordings/p1_call.mp4", "",                 "", "", "", ""],
            ["4",  "START_RECORD", "Phone2", "",             "recordings/p2_call.mp4", "",                 "", "", "", ""],
            ["5",  "WAIT",         "Phone1", "10",           "",                    "",                   "", "", "", ""],
            ["6",  "END_CALL",     "Phone1", "",             "",                    "",                   "", "", "", ""],
            ["7",  "STOP_RECORD",  "Phone1", "",             "recordings/p1_call.mp4", "",                 "", "", "", ""],
            ["8",  "STOP_RECORD",  "Phone2", "",             "recordings/p2_call.mp4", "",                 "", "", "", ""],
            # ── SMS flow ───────────────────────────────────────────────────────────
            # Phone1 sends SMS → check it arrived on Phone2
            ["9",  "SMS",          "Phone1", "+97699002222", "Hello from Phone1",   "",                   "", "", "", ""],
            ["10", "WAIT",         "Phone1", "5",            "",                    "",                   "", "", "", ""],
            ["11", "CHECK_SMS",    "Phone2", "+97699001111", "Hello from Phone1",   "Hello from Phone1",  "", "", "", ""],
            # ── USSD flow ──────────────────────────────────────────────────────────
            ["12", "USSD",         "Phone1", "*100#",        "",                    "",                   "", "", "", ""],
        ]

        data_align = Alignment(vertical="center")
        for row_idx, row_data in enumerate(examples, start=2):
            for col_idx, val in enumerate(row_data, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.alignment = data_align

        # Freeze header row
        ws.freeze_panes = "A2"

        wb.save(file_path)
        print(f"Template written to '{file_path}'")
