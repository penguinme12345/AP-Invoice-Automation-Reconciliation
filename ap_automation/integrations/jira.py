"""Exception tickets in Jira.

``build_ticket`` turns a failed match into a ticket request and applies the
routing rules (see docs/jira_automation.md):

* component + assignee by the PO's department (AP owner per department)
* ``high-dollar`` label and Highest priority at or above the threshold
* High priority for duplicates and unapproved vendors (risk of paying twice
  or paying a vendor that failed compliance)

In a real Jira site those rules live in Jira Automation, keyed on the
component and labels this code sets. ``MockJira`` applies the same rules
locally so the demo can show the routed result with no Jira account.
``JiraCloud`` calls the REST API v3.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Protocol

CODE_TEXT = {
    "EXTRACTION_FAILED": "Invoice could not be read reliably; enter or verify the fields by hand.",
    "MISSING_PO": "No PO number on the invoice. Ask the vendor for the PO or find it by vendor and amount.",
    "PO_NOT_FOUND": "PO number on the invoice does not exist in the ERP. Check for a typo or another entity.",
    "VENDOR_NOT_APPROVED": "Vendor is not approved (pending onboarding or blocked). Do not pay until Procurement clears it.",
    "VENDOR_PO_MISMATCH": "The PO belongs to a different vendor.",
    "CURRENCY_MISMATCH": "Invoice currency differs from the PO currency. Amounts are not comparable; confirm the "
                         "agreed currency with the buyer.",
    "DUPLICATE_INVOICE": "This invoice number was already received from this vendor. Likely a resend; confirm and reject.",
    "AMOUNT_OUT_OF_TOLERANCE": "Invoice amount differs from PO price x billed quantity by more than the tolerance.",
    "QUANTITY_MISMATCH": "Billed quantity is more than was received (or ordered). Check with the receiving team.",
}


@dataclass
class TicketRequest:
    invoice_id: int
    summary: str
    description: str
    exception_codes: list[str]
    priority: str
    component: str
    assignee: str
    labels: list[str]
    amount: Optional[float]
    created_at: str
    fields: dict = field(default_factory=dict)


class JiraClient(Protocol):
    def create_issue(self, ticket: TicketRequest) -> Optional[str]: ...
    def add_comment(self, key: str, body: str) -> None: ...


def build_ticket(conn: sqlite3.Connection, invoice_id: int, codes: list[str], created_at: str,
                 high_dollar: float, project_key: str = "AP") -> TicketRequest:
    row = conn.execute(
        """SELECT i.*, d.dept_id, d.name AS dept_name, d.jira_component, d.ap_owner,
                  v.name AS vendor_master_name, v.status AS vendor_status, m.amount_variance_pct, m.expected_amount
           FROM invoices i
           LEFT JOIN purchase_orders p ON p.po_number = i.po_number
           LEFT JOIN departments d ON d.dept_id = p.dept_id
           LEFT JOIN vendors v ON v.vendor_id = i.vendor_id
           LEFT JOIN match_results m ON m.invoice_id = i.invoice_id
           WHERE i.invoice_id = ?""", (invoice_id,)).fetchone()

    amount = row["total"]
    vendor = row["vendor_master_name"] or row["vendor_name_raw"] or "Unknown vendor"
    component = row["jira_component"] or "AP-Triage"
    assignee = row["ap_owner"] or "ap.triage"

    if amount is not None and amount >= high_dollar:
        priority = "Highest"
    elif {"DUPLICATE_INVOICE", "VENDOR_NOT_APPROVED"} & set(codes):
        priority = "High"
    else:
        priority = "Medium"

    labels = ["ap-exception"] + [c.lower().replace("_", "-") for c in codes]
    if amount is not None and amount >= high_dollar:
        labels.append("high-dollar")

    head = codes[0].replace("_", " ").title()
    summary = f"[{head}] {vendor} invoice {row['invoice_number'] or row['source_file']}"
    if amount is not None:
        summary += f" (${amount:,.2f})"

    lines = [
        f"Source file: {row['source_file']}",
        f"Vendor: {vendor} (status: {row['vendor_status'] or 'not in vendor master'})",
        f"Invoice number: {row['invoice_number'] or '-'}",
        f"PO number: {row['po_number'] or '-'}",
        f"Department: {row['dept_name'] or 'Unknown - triage'}",
        f"Invoice total: {'$%s' % format(amount, ',.2f') if amount is not None else '-'}",
    ]
    if row["expected_amount"] is not None:
        lines.append(f"PO-priced value of billed lines: ${row['expected_amount']:,.2f} "
                     f"(variance {row['amount_variance_pct'] * 100:+.2f}%)")
    if row["extraction_error"]:
        lines.append(f"Extraction error: {row['extraction_error']}")
    lines.append("")
    lines.append("What failed:")
    lines += [f"- {c}: {CODE_TEXT.get(c, c)}" for c in codes]

    return TicketRequest(
        invoice_id=invoice_id, summary=summary[:250], description="\n".join(lines), exception_codes=codes,
        priority=priority, component=component, assignee=assignee, labels=labels, amount=amount,
        created_at=created_at,
        fields={"project": {"key": project_key}, "issuetype": {"name": "Task"}},
    )


class MockJira:
    """Stores tickets in the exception_tickets table and a JSONL file."""

    def __init__(self, conn: sqlite3.Connection, out_dir: Path, project_key: str = "AP"):
        self.conn = conn
        self.project_key = project_key
        self.log = Path(out_dir) / "jira_tickets.jsonl"
        self.log.parent.mkdir(parents=True, exist_ok=True)

    def create_issue(self, ticket: TicketRequest) -> str:
        n = self.conn.execute("SELECT COUNT(*) FROM exception_tickets").fetchone()[0] + 1
        key = f"{self.project_key}-{n}"
        _store(self.conn, key, ticket)
        with self.log.open("a") as fh:
            fh.write(json.dumps({"key": key, **asdict(ticket)}) + "\n")
        return key

    def add_comment(self, key: str, body: str) -> None:
        with self.log.open("a") as fh:
            fh.write(json.dumps({"key": key, "comment": body}) + "\n")


class JiraCloud:
    """Jira Cloud REST API v3 (basic auth with an API token)."""

    def __init__(self, base_url: str, email: str, api_token: str, conn: sqlite3.Connection, project_key: str = "AP"):
        if not (base_url and email and api_token):
            raise ValueError("JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN are required for live Jira mode")
        import requests

        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.conn = conn
        self.project_key = project_key

    @staticmethod
    def _adf(text: str) -> dict:
        return {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else {"type": "paragraph"}
            for line in text.split("\n")
        ]}

    def create_issue(self, ticket: TicketRequest) -> str:
        payload = {"fields": {
            **ticket.fields,
            "summary": ticket.summary,
            "description": self._adf(ticket.description),
            "priority": {"name": ticket.priority},
            "labels": ticket.labels,
            "components": [{"name": ticket.component}],
        }}
        r = self.session.post(f"{self.base}/rest/api/3/issue", data=json.dumps(payload), timeout=30)
        r.raise_for_status()
        key = r.json()["key"]
        _store(self.conn, key, ticket)
        return key

    def add_comment(self, key: str, body: str) -> None:
        r = self.session.post(f"{self.base}/rest/api/3/issue/{key}/comment",
                              data=json.dumps({"body": self._adf(body)}), timeout=30)
        r.raise_for_status()


class LocalRecordJira:
    """Used when an orchestrator (n8n / Workato) creates the Jira issue itself.

    The ticket is recorded locally as LOCAL-<invoice_id> so the dashboard and SLA
    reporting still see it; the orchestrator then calls
    `python -m ap_automation link-ticket <invoice_id> <JIRA-KEY>`. Returns None so the
    JSON outcome shows the issue still has to be created."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_issue(self, ticket: TicketRequest) -> None:
        _store(self.conn, f"LOCAL-{ticket.invoice_id}", ticket)
        return None

    def add_comment(self, key: str, body: str) -> None:
        return None


def _store(conn: sqlite3.Connection, key: str, t: TicketRequest) -> None:
    conn.execute(
        """INSERT INTO exception_tickets (ticket_key, invoice_id, summary, description, exception_codes, priority,
               component, assignee, labels, amount, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'Open', ?)""",
        (key, t.invoice_id, t.summary, t.description, ",".join(t.exception_codes), t.priority, t.component,
         t.assignee, ",".join(t.labels), t.amount, t.created_at))


def send_sla_reminders(conn: sqlite3.Connection, jira: JiraClient, as_of: datetime, sla_days: int = 3) -> list[str]:
    """Reminder for tickets open longer than the SLA (mirrors the Jira scheduled rule).

    A ticket is reminded at most once per SLA window."""
    cutoff = (as_of - timedelta(days=sla_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT ticket_key, assignee, created_at, last_reminder_at FROM exception_tickets
           WHERE status <> 'Done' AND created_at <= ?
             AND (last_reminder_at IS NULL OR last_reminder_at <= ?)""", (cutoff, cutoff)).fetchall()
    keys = []
    for r in rows:
        age = (as_of - datetime.fromisoformat(r["created_at"])).days
        jira.add_comment(r["ticket_key"], f"Reminder for {r['assignee']}: this AP exception has been open "
                                          f"{age} days (SLA {sla_days} days).")
        conn.execute("UPDATE exception_tickets SET last_reminder_at = ? WHERE ticket_key = ?",
                     (as_of.isoformat(timespec="seconds"), r["ticket_key"]))
        keys.append(r["ticket_key"])
    conn.commit()
    return keys
