"""Orchestration: intake -> extract -> validate -> store -> three-way match -> route.

Each invoice is processed end to end before the next one, in arrival order,
so duplicate detection sees earlier invoices exactly as it would in
production. Side effects (Jira, Sheets) go through the interfaces in
``integrations`` and can be switched to mocks or turned off entirely when an
external orchestrator such as n8n or Workato owns them.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import db
from .config import Settings
from .extraction import InvoiceExtractor
from .integrations.drive import InboxFile, InvoiceSource, LocalFolderSource
from .integrations.jira import JiraClient, MockJira, NullJira, build_ticket, send_sla_reminders
from .integrations.sheets import ApprovedSheet, MockSheet, NullSheet

_SUFFIX = re.compile(r"\b(inc|llc|ltd|llp|co|corp|corporation|company|limited)\b\.?", re.I)


def normalise_vendor(name: str) -> str:
    s = _SUFFIX.sub("", name.lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def resolve_vendor(conn: sqlite3.Connection, name: Optional[str]) -> Optional[str]:
    """Map the vendor name printed on the invoice to a vendor_id in the master."""
    if not name:
        return None
    target = normalise_vendor(name)
    rows = conn.execute("SELECT vendor_id, name FROM vendors").fetchall()
    best, score = None, 0.0
    for r in rows:
        cand = normalise_vendor(r["name"])
        if cand == target:
            return r["vendor_id"]
        s = difflib.SequenceMatcher(None, cand, target).ratio()
        if s > score:
            best, score = r["vendor_id"], s
    return best if score >= 0.88 else None


@dataclass
class InvoiceOutcome:
    file: str
    invoice_id: int
    status: str
    exception_codes: list[str]
    ticket_key: Optional[str] = None
    ticket: Optional[dict] = None
    sheet_row: Optional[list] = None
    processing_ms: float = 0.0


@dataclass
class RunSummary:
    files_seen: int = 0
    files_new: int = 0
    matched: int = 0
    exceptions: int = 0
    outcomes: list[InvoiceOutcome] = field(default_factory=list)
    reminders_sent: list[str] = field(default_factory=list)
    paid: int = 0


class Pipeline:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, extractor: InvoiceExtractor,
                 source: InvoiceSource, jira: JiraClient, sheet: ApprovedSheet, replay_clock: bool = True):
        self.s = settings
        self.conn = conn
        self.extractor = extractor
        self.source = source
        self.jira = jira
        self.sheet = sheet
        # replay_clock: event timestamps come from the file's arrival time (demo replays
        # ~4 months of invoices). Off in production: timestamps are "now".
        self.replay_clock = replay_clock
        self.match_sql = db.read_sql("three_way_match.sql")

    # ------------------------------------------------------------------ steps
    def _ingest(self, f: InboxFile, sha: str) -> tuple[int, float]:
        t0 = time.perf_counter()
        res = self.extractor.extract(f.path)
        inv = res.invoice
        raw_vendor = inv.vendor_name if inv else (res.raw.vendor_name if res.raw else None)
        cur = self.conn.execute(
            """INSERT INTO invoices (source_file, file_sha256, received_at, extractor, extraction_status,
                   extraction_error, vendor_name_raw, vendor_id, invoice_number, po_number, invoice_date,
                   due_date, currency, subtotal, tax, total, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'RECEIVED')""",
            (f.name, sha, f.received_at.isoformat(timespec="seconds"), res.extractor,
             "OK" if inv else "FAILED", res.error, raw_vendor, resolve_vendor(self.conn, raw_vendor),
             inv.invoice_number if inv else (res.raw.invoice_number if res.raw else None),
             inv.po_number if inv else None,
             inv.invoice_date.isoformat() if inv else None,
             inv.due_date.isoformat() if inv and inv.due_date else None,
             inv.currency if inv else None,
             float(inv.subtotal) if inv else None, float(inv.tax) if inv else None,
             float(inv.total) if inv else (res.raw.total if res.raw else None)))
        invoice_id = cur.lastrowid
        if inv:
            self.conn.executemany(
                "INSERT INTO invoice_lines VALUES (?,?,?,?,?,?,?)",
                [(invoice_id, li.line_no, li.sku, li.description, float(li.quantity), float(li.unit_price),
                  float(li.amount)) for li in inv.line_items])
        return invoice_id, t0

    def _match(self, now_iso: str) -> list[sqlite3.Row]:
        rows = self.conn.execute(self.match_sql, {"tolerance": self.s.amount_tolerance_pct}).fetchall()
        for r in rows:
            self.conn.execute(
                """INSERT INTO match_results VALUES (:invoice_id,:extraction_failed,:missing_po,:po_not_found,
                       :vendor_not_approved,:vendor_po_mismatch,:duplicate_invoice,:amount_out_of_tolerance,
                       :quantity_mismatch,:expected_amount,:amount_variance_pct,:exception_codes,:matched,:matched_at)""",
                {**dict(r), "matched": int(r["exception_codes"] == ""), "matched_at": now_iso})
        return rows

    def _route(self, invoice_id: int, codes: list[str], now_iso: str) -> InvoiceOutcome:
        row = self.conn.execute(
            """SELECT i.*, d.name AS dept FROM invoices i
               LEFT JOIN purchase_orders p ON p.po_number = i.po_number
               LEFT JOIN departments d ON d.dept_id = p.dept_id WHERE invoice_id = ?""", (invoice_id,)).fetchone()
        out = InvoiceOutcome(file=row["source_file"], invoice_id=invoice_id,
                             status="APPROVED" if not codes else "EXCEPTION", exception_codes=codes)
        if not codes:
            vendor = self.conn.execute("SELECT name FROM vendors WHERE vendor_id = ?", (row["vendor_id"],)).fetchone()
            sheet_row = [invoice_id, vendor["name"], row["invoice_number"], row["po_number"], row["dept"],
                         round(row["total"], 2), row["due_date"], now_iso]
            self.conn.execute("INSERT INTO approved_for_payment VALUES (?,?,?,?,?,?,?)",
                              (invoice_id, vendor["name"], row["invoice_number"], row["po_number"], row["total"],
                               row["due_date"], now_iso))
            self.sheet.append(sheet_row)
            out.sheet_row = sheet_row
        else:
            ticket = build_ticket(self.conn, invoice_id, codes, now_iso, self.s.high_dollar_threshold,
                                  self.s.jira_project_key)
            out.ticket = ticket.__dict__
            out.ticket_key = self.jira.create_issue(ticket)
        self.conn.execute("UPDATE invoices SET status = ? WHERE invoice_id = ?", (out.status, invoice_id))
        return out

    # -------------------------------------------------------------------- run
    def run(self) -> RunSummary:
        summary = RunSummary()
        started = datetime.now().isoformat(timespec="seconds")
        run_id = self.conn.execute("INSERT INTO pipeline_runs (started_at, extractor) VALUES (?, ?)",
                                   (started, self.extractor.mode)).lastrowid
        files = self.source.list_files()
        summary.files_seen = len(files)
        for f in files:
            sha = hashlib.sha256(f.path.read_bytes()).hexdigest()
            if self.conn.execute("SELECT 1 FROM invoices WHERE file_sha256 = ?", (sha,)).fetchone():
                continue  # already ingested (idempotent re-runs)
            summary.files_new += 1
            invoice_id, t0 = self._ingest(f, sha)
            clock = f.received_at if self.replay_clock else datetime.now()
            rows = self._match(clock.isoformat(timespec="seconds"))
            codes = next(r["exception_codes"] for r in rows if r["invoice_id"] == invoice_id)
            codes = [c for c in codes.split(",") if c]
            elapsed_ms = (time.perf_counter() - t0) * 1000
            processed_at = (clock + timedelta(milliseconds=elapsed_ms)).isoformat(timespec="seconds")
            out = self._route(invoice_id, codes, processed_at)
            out.processing_ms = round((time.perf_counter() - t0) * 1000, 1)
            self.conn.execute("UPDATE invoices SET processed_at = ?, processing_ms = ? WHERE invoice_id = ?",
                              (processed_at, out.processing_ms, invoice_id))
            self.conn.commit()
            summary.outcomes.append(out)
            if codes:
                summary.exceptions += 1
            else:
                summary.matched += 1
        self.conn.execute(
            "UPDATE pipeline_runs SET finished_at=?, files_seen=?, files_new=?, matched=?, exceptions=? WHERE run_id=?",
            (datetime.now().isoformat(timespec="seconds"), summary.files_seen, summary.files_new, summary.matched,
             summary.exceptions, run_id))
        self.conn.commit()
        return summary


def payment_run(conn: sqlite3.Connection, as_of: str) -> int:
    """Treasury pays approved invoices on their due date (simulated)."""
    rows = conn.execute(
        "SELECT invoice_id, total, due_date FROM invoices WHERE status = 'APPROVED' AND due_date <= ?",
        (as_of,)).fetchall()
    for r in rows:
        conn.execute("INSERT OR IGNORE INTO payments VALUES (?,?,?)", (r["invoice_id"], r["due_date"], r["total"]))
        conn.execute("UPDATE invoices SET status = 'PAID' WHERE invoice_id = ?", (r["invoice_id"],))
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------- setup
def build_components(settings: Settings, conn: sqlite3.Connection, integrations: str = "mock",
                     arrival_times: dict | None = None):
    """Wire the configured implementations. integrations='none' skips Jira/Sheets side effects."""
    if settings.drive_mode == "live":
        from .integrations.drive import GoogleDriveSource
        source = GoogleDriveSource(settings.drive_folder_id, settings.google_service_account_file,
                                   settings.inbox_dir / "_drive_cache")
    else:
        source = LocalFolderSource(settings.inbox_dir, arrival_times)

    if integrations == "none":
        jira, sheet = NullJira(), NullSheet()
    else:
        if settings.jira_mode == "live":
            from .integrations.jira import JiraCloud
            jira = JiraCloud(settings.jira_base_url, settings.jira_email, settings.jira_api_token, conn,
                             settings.jira_project_key)
        else:
            jira = MockJira(conn, settings.output_dir, settings.jira_project_key)
        if settings.sheets_mode == "live":
            from .integrations.sheets import GoogleSheet
            sheet = GoogleSheet(settings.sheet_id, settings.sheet_tab, settings.google_service_account_file)
        else:
            sheet = MockSheet(settings.output_dir)
    extractor = InvoiceExtractor(settings.extractor, settings.llm_model)
    return extractor, source, jira, sheet


def load_arrival_times(settings: Settings) -> dict[str, str]:
    if settings.manifest_path.exists():
        return {m["file_name"]: m["received_at"] for m in json.loads(settings.manifest_path.read_text())}
    return {}


def run_sla_job(conn: sqlite3.Connection, jira: JiraClient, settings: Settings) -> list[str]:
    as_of = datetime.combine(settings.as_of, datetime.min.time()) + timedelta(hours=9)
    return send_sla_reminders(conn, jira, as_of, settings.sla_days)


def set_as_of(conn: sqlite3.Connection, as_of: str) -> None:
    conn.execute("INSERT OR REPLACE INTO app_settings VALUES ('as_of', ?)", (as_of,))
    conn.commit()


def clear_outputs(out_dir: Path) -> None:
    for name in ("approved_for_payment.csv", "jira_tickets.jsonl"):
        p = Path(out_dir) / name
        if p.exists():
            p.unlink()
