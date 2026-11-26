import csv
import json
from datetime import datetime

import pytest

from ap_automation import db
from ap_automation.dashboard import build_dashboard
from ap_automation.evaluate import VIEWS_FOR_BI, evaluate, export_bi
from ap_automation.integrations.jira import MockJira, send_sla_reminders
from ap_automation.pipeline import normalise_vendor, resolve_vendor

from .conftest import make_settings, run_pipeline, setup_world


def codes_by_file(conn):
    return {r["source_file"]: {c for c in r["exception_codes"].split(",") if c}
            for r in conn.execute("SELECT i.source_file, m.exception_codes FROM invoices i "
                                  "JOIN match_results m USING (invoice_id)")}


def test_every_planted_case_gets_the_expected_codes(processed):
    got = codes_by_file(processed["conn"])
    wrong = [(s.file_name, s.scenario, s.expected_codes, sorted(got[s.file_name]))
             for s in processed["world"].invoices if set(s.expected_codes) != got[s.file_name]]
    assert not wrong


def test_metrics_no_false_approvals(processed):
    m = evaluate(processed["conn"], processed["settings"].manifest_path)
    assert m["invoices"] == len(processed["world"].invoices)
    assert m["false_approvals"] == []
    assert m["problem_detection_rate"] == 1.0
    assert m["clean_auto_approval_rate"] == 1.0


def test_duplicate_flagged_and_original_approved(processed):
    conn = processed["conn"]
    dup = next(s for s in processed["world"].invoices if s.scenario == "duplicate")
    rows = conn.execute("SELECT i.invoice_id, m.exception_codes FROM invoices i JOIN match_results m USING (invoice_id) "
                        "WHERE invoice_number = ? ORDER BY invoice_id", (dup.invoice_number,)).fetchall()
    assert len(rows) == 2
    assert rows[0]["exception_codes"] == ""
    # the resend is a duplicate, and it also bills units that were already billed
    assert rows[1]["exception_codes"] == "DUPLICATE_INVOICE,QUANTITY_MISMATCH"


def test_missing_po_goes_to_jira_not_payment(processed):
    conn = processed["conn"]
    spec = next(s for s in processed["world"].invoices if s.scenario == "missing_po")
    inv = conn.execute("SELECT * FROM invoices WHERE source_file = ?", (spec.file_name,)).fetchone()
    assert inv["status"] == "EXCEPTION"
    t = conn.execute("SELECT * FROM exception_tickets WHERE invoice_id = ?", (inv["invoice_id"],)).fetchone()
    assert "MISSING_PO" in t["exception_codes"] and "missing-po" in t["labels"]
    assert conn.execute("SELECT 1 FROM approved_for_payment WHERE invoice_id = ?", (inv["invoice_id"],)).fetchone() is None


def test_blurry_scan_routed_to_triage(processed):
    conn = processed["conn"]
    spec = next(s for s in processed["world"].invoices if s.scenario == "poor_scan")
    row = conn.execute("""SELECT i.extraction_status, t.component FROM invoices i
                          JOIN exception_tickets t USING (invoice_id) WHERE source_file = ?""",
                       (spec.file_name,)).fetchone()
    assert row["extraction_status"] == "FAILED"
    assert row["component"] == "AP-Triage"


def test_within_tolerance_approved_but_tighter_tolerance_flags_it(tmp_path):
    s = make_settings(tmp_path)
    s.amount_tolerance_pct = 0.001
    conn, world = setup_world(s)
    run_pipeline(s, conn)
    got = codes_by_file(conn)
    for spec in world.invoices:
        if spec.scenario == "within_tolerance":
            assert got[spec.file_name] == {"AMOUNT_OUT_OF_TOLERANCE"}


def test_rerun_is_idempotent(tmp_path):
    s = make_settings(tmp_path)
    conn, _ = setup_world(s)
    first, _ = run_pipeline(s, conn)
    second, _ = run_pipeline(s, conn)
    assert first.files_new == s.n_invoices
    assert second.files_new == 0
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == s.n_invoices


def test_sheet_has_one_row_per_approved_invoice(processed):
    s, conn = processed["settings"], processed["conn"]
    with (s.output_dir / "approved_for_payment.csv").open() as fh:
        rows = list(csv.reader(fh))[1:]
    approved = conn.execute("SELECT COUNT(*) FROM approved_for_payment").fetchone()[0]
    assert len(rows) == approved == processed["summary"].matched


def test_ticket_routing_rules(processed):
    conn = processed["conn"]
    for t in conn.execute("""SELECT t.*, d.jira_component, d.ap_owner FROM exception_tickets t
                             JOIN invoices i USING (invoice_id)
                             LEFT JOIN purchase_orders p ON p.po_number = i.po_number
                             LEFT JOIN departments d ON d.dept_id = p.dept_id"""):
        if t["amount"] is not None and t["amount"] >= 10000:
            assert t["priority"] == "Highest" and "high-dollar" in t["labels"]
        elif "DUPLICATE_INVOICE" in t["exception_codes"] or "VENDOR_NOT_APPROVED" in t["exception_codes"]:
            assert t["priority"] == "High"
        else:
            assert t["priority"] == "Medium"
        assert t["component"] == (t["jira_component"] or "AP-Triage")
        assert t["assignee"] == (t["ap_owner"] or "ap.triage")


def test_sla_reminder_after_three_days_only_once(tmp_path):
    s = make_settings(tmp_path)
    conn, _ = setup_world(s)
    _, jira = run_pipeline(s, conn)
    newest = conn.execute("SELECT MAX(created_at) FROM exception_tickets").fetchone()[0]
    as_of = datetime.fromisoformat(newest)
    assert send_sla_reminders(conn, jira, as_of, 3) and all(
        r[0] is not None for r in conn.execute(
            "SELECT last_reminder_at FROM exception_tickets WHERE created_at <= datetime(?, '-3 days')", (newest,)))
    # the ticket created "now" is inside the SLA
    assert conn.execute("SELECT last_reminder_at FROM exception_tickets WHERE created_at = ?",
                        (newest,)).fetchone()[0] is None
    assert send_sla_reminders(conn, jira, as_of, 3) == []


def test_payment_run_pays_only_due_approved(processed):
    conn, s = processed["conn"], processed["settings"]
    bad = conn.execute("SELECT COUNT(*) FROM invoices WHERE status = 'PAID' AND (due_date > ? OR invoice_id IN "
                       "(SELECT invoice_id FROM exception_tickets))", (s.as_of.isoformat(),)).fetchone()[0]
    assert bad == 0
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] > 0


@pytest.mark.parametrize("view", VIEWS_FOR_BI)
def test_reporting_views_return_rows(processed, view):
    assert processed["conn"].execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0] > 0


def test_budget_vs_actual_has_every_department(processed):
    depts = {r[0] for r in processed["conn"].execute("SELECT DISTINCT dept_id FROM v_budget_vs_actual")}
    assert len(depts) == 7


def test_bi_export_and_dashboard(processed, tmp_path):
    conn = processed["conn"]
    paths = export_bi(conn, tmp_path)
    assert len(paths) == len(VIEWS_FOR_BI) and all(p.stat().st_size > 0 for p in paths)
    m = evaluate(conn, processed["settings"].manifest_path)
    html = build_dashboard(conn, tmp_path / "d.html", m).read_text()
    for heading in ("Unpaid invoices by age", "Spend by vendor", "Spend by department", "Budget vs actual",
                    "Exception rate", "Processing time", "Open exception queue"):
        assert heading in html


@pytest.mark.parametrize("printed,master", [
    ("Cobalt Office Supply LLC", "Cobalt Office Supply"),
    ("Redwood Print & Packaging Inc.", "Redwood Print & Packaging"),
    ("Brightline IT Hardware, Inc", "Brightline IT Hardware"),
])
def test_vendor_name_resolution(processed, printed, master):
    conn = processed["conn"]
    vid = resolve_vendor(conn, printed)
    assert conn.execute("SELECT name FROM vendors WHERE vendor_id = ?", (vid,)).fetchone()[0] == master


def test_unknown_vendor_not_resolved(processed):
    assert resolve_vendor(processed["conn"], "Totally Different Corp") is None
    assert normalise_vendor("Acme, Inc.") == "acme"


def test_cli_json_mode_for_orchestrators(tmp_path, capsys, monkeypatch):
    from ap_automation.__main__ import main

    s = make_settings(tmp_path)
    setup_world(s)
    for k, v in {"AP_DB_PATH": s.db_path, "AP_INBOX_DIR": s.inbox_dir, "AP_OUTPUT_DIR": s.output_dir,
                 "AP_SEED": s.seed}.items():
        monkeypatch.setenv(k, str(v))
    capsys.readouterr()
    main(["run", "--json", "--integrations", "none"])
    out = json.loads(capsys.readouterr().out)
    assert len(out) == s.n_invoices
    exc = [o for o in out if o["status"] == "EXCEPTION"]
    assert exc and all(o["ticket"] and o["ticket_key"] is None for o in exc)
    assert all(o["sheet_row"] for o in out if o["status"] == "APPROVED")

    # exceptions are still recorded locally for reporting, then linked to the real Jira key
    conn = db.connect(s.db_path)
    assert conn.execute("SELECT COUNT(*) FROM exception_tickets WHERE ticket_key LIKE 'LOCAL-%'").fetchone()[0] == len(exc)
    # a plain `run` never marks anything paid; that needs a real payment confirmation
    assert conn.execute("SELECT COUNT(*) FROM invoices WHERE status = 'PAID'").fetchone()[0] == 0
    first = exc[0]["invoice_id"]
    assert main(["link-ticket", str(first), "AP-501"]) == 0
    assert conn.execute("SELECT ticket_key FROM exception_tickets WHERE invoice_id = ?", (first,)).fetchone()[0] == "AP-501"
    assert main(["link-ticket", str(first), "AP-502"]) == 1  # already linked


def _add_invoice(conn, invoice_id_hint, po, vendor_id, number, lines, currency="USD", date="2026-11-10"):
    """Insert a hand-built invoice (bypassing extraction) for targeted match tests."""
    subtotal = round(sum(q * p for _, q, p in lines), 2)
    cur = conn.execute(
        """INSERT INTO invoices (source_file, file_sha256, received_at, extraction_status, vendor_name_raw, vendor_id,
               invoice_number, po_number, invoice_date, due_date, currency, subtotal, tax, total, status)
           VALUES (?,?,?, 'OK', 'x', ?,?,?,?,?,?,?,0,?, 'RECEIVED')""",
        (f"{invoice_id_hint}.pdf", invoice_id_hint, "2026-11-19T10:00:00", vendor_id, number, po, date, "2026-12-10",
         currency, subtotal, subtotal))
    conn.executemany("INSERT INTO invoice_lines VALUES (?,?,?,?,?,?,?)",
                     [(cur.lastrowid, ln, None, "item", q, p, round(q * p, 2)) for ln, q, p in lines])
    return cur.lastrowid


def _match_new(conn, tolerance=0.02):
    rows = conn.execute(db.read_sql("three_way_match.sql"), {"tolerance": tolerance}).fetchall()
    for r in rows:
        conn.execute("""INSERT INTO match_results (invoice_id, extraction_failed, missing_po, po_not_found,
                            vendor_not_approved, vendor_po_mismatch, currency_mismatch, duplicate_invoice,
                            amount_out_of_tolerance, quantity_mismatch, expected_amount, amount_variance_pct,
                            exception_codes, matched, matched_at)
                        VALUES (:invoice_id,:extraction_failed,:missing_po,:po_not_found,:vendor_not_approved,
                            :vendor_po_mismatch,:currency_mismatch,:duplicate_invoice,:amount_out_of_tolerance,
                            :quantity_mismatch,:expected_amount,:amount_variance_pct,:exception_codes,
                            :exception_codes = '', 'now')""", dict(r))
    return {r["invoice_id"]: r["exception_codes"] for r in rows}


@pytest.fixture
def fresh(tmp_path):
    s = make_settings(tmp_path)
    conn, world = setup_world(s)
    # an open PO with a full receipt and no invoices yet
    po = conn.execute("""SELECT p.po_number, p.vendor_id FROM purchase_orders p JOIN vendors v USING (vendor_id)
                         WHERE v.status = 'APPROVED' AND p.po_number IN (SELECT po_number FROM goods_receipts)
                           AND p.po_number NOT IN (SELECT po_number FROM po_lines WHERE quantity < 2)
                         LIMIT 1""").fetchone()
    lines = conn.execute("SELECT line_no, quantity, unit_price FROM po_lines WHERE po_number = ? ORDER BY line_no",
                         (po["po_number"],)).fetchall()
    return conn, po, [(l["line_no"], l["quantity"], l["unit_price"]) for l in lines]


def test_partial_invoices_cannot_bill_more_than_received_in_total(fresh):
    conn, po, lines = fresh
    first_half = [(ln, q // 2 or 1, p) for ln, q, p in lines]
    rest = [(ln, q - (q // 2 or 1), p) for ln, q, p in lines]
    a = _add_invoice(conn, "a", po["po_number"], po["vendor_id"], "P-1", first_half)
    assert _match_new(conn)[a] == ""
    b = _add_invoice(conn, "b", po["po_number"], po["vendor_id"], "P-2", rest, date="2026-11-12")
    assert _match_new(conn)[b] == "", "billing the remainder is fine"
    c = _add_invoice(conn, "c", po["po_number"], po["vendor_id"], "P-3", first_half, date="2026-11-14")
    assert _match_new(conn)[c] == "QUANTITY_MISMATCH", "a third invoice over-bills the PO"


def test_currency_mismatch_blocks_approval(fresh):
    conn, po, lines = fresh
    i = _add_invoice(conn, "eur", po["po_number"], po["vendor_id"], "EU-1", lines, currency="EUR")
    assert _match_new(conn)[i] == "CURRENCY_MISMATCH"


def test_sheet_values_cannot_inject_formulas(tmp_path):
    from ap_automation.integrations.sheets import MockSheet, safe_cell

    assert safe_cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert safe_cell("INV-1") == "INV-1" and safe_cell(-5.0) == -5.0
    sheet = MockSheet(tmp_path)
    sheet.append([1, "Vendor", "=1+1", "PO-10001", "IT", 10.0, "USD", "2026-12-01", "now"])
    assert "'=1+1" in sheet.path.read_text()
