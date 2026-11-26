import csv
import json
from datetime import datetime

import pytest

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
    assert rows[1]["exception_codes"] == "DUPLICATE_INVOICE"


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
