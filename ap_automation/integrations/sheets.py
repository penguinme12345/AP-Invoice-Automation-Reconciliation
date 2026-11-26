"""The "Approved for payment" sheet.

``MockSheet`` appends to outputs/approved_for_payment.csv (opens in Excel or
imports into Google Sheets). ``GoogleSheet`` appends rows with gspread.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Protocol

HEADER = ["Invoice ID", "Vendor", "Invoice #", "PO #", "Department", "Amount", "Currency", "Due date", "Approved at"]

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(v):
    """Neutralise text a spreadsheet would treat as a formula.

    Invoice numbers and vendor names come from vendor documents, so a value such as
    '=HYPERLINK(...)' must land as text, not run in the shared payment workbook."""
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


class ApprovedSheet(Protocol):
    def append(self, row: list) -> None: ...


class MockSheet:
    def __init__(self, out_dir: Path):
        self.path = Path(out_dir) / "approved_for_payment.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="") as fh:
                csv.writer(fh).writerow(HEADER)

    def append(self, row: list) -> None:
        with self.path.open("a", newline="") as fh:
            csv.writer(fh).writerow([safe_cell(v) for v in row])


class GoogleSheet:
    """Needs `pip install -e .[google]` and the sheet shared with the service account."""

    def __init__(self, sheet_id: str, tab: str, service_account_file: str):
        if not sheet_id or not service_account_file:
            raise ValueError("APPROVED_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE are required for live Sheets mode")
        import gspread

        gc = gspread.service_account(filename=service_account_file)
        sh = gc.open_by_key(sheet_id)
        try:
            self.ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            self.ws = sh.add_worksheet(title=tab, rows=1000, cols=len(HEADER))
            self.ws.append_row(HEADER)

    def append(self, row: list) -> None:
        # RAW: values are stored exactly as given and never parsed as formulas.
        self.ws.append_row(row, value_input_option="RAW")


class NullSheet:
    def append(self, row: list) -> None:
        return None
