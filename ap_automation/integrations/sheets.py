"""The "Approved for payment" sheet.

``MockSheet`` appends to outputs/approved_for_payment.csv (opens in Excel or
imports into Google Sheets). ``GoogleSheet`` appends rows with gspread.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Protocol

HEADER = ["Invoice ID", "Vendor", "Invoice #", "PO #", "Department", "Amount", "Due date", "Approved at"]


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
            csv.writer(fh).writerow(row)


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
        self.ws.append_row(row, value_input_option="USER_ENTERED")


class NullSheet:
    def append(self, row: list) -> None:
        return None
