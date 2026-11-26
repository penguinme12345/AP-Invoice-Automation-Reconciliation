"""Command line entry point.

    python -m ap_automation demo          # everything, end to end, offline
    python -m ap_automation setup         # synthetic ERP data + invoice PDFs
    python -m ap_automation run [--json] [--integrations none]
    python -m ap_automation sla           # 3-day reminder job
    python -m ap_automation report        # evaluation + BI exports + dashboard
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import db
from .config import Settings
from .dashboard import build_dashboard
from .datagen import generate_world, load_erp
from .evaluate import evaluate, export_bi, report_markdown
from .invoice_pdfs import write_invoices
from .pipeline import (Pipeline, build_components, clear_outputs, load_arrival_times, payment_run,
                       run_sla_job, set_as_of)


def cmd_setup(s: Settings, conn) -> None:
    world = generate_world(s.seed, s.n_invoices, s.as_of)
    db.reset_ap_tables(conn)
    load_erp(conn, world)
    clear_outputs(s.output_dir)
    set_as_of(conn, s.as_of.isoformat())
    write_invoices(world.invoices, s.inbox_dir, s.manifest_path, s.seed)
    print(f"ERP loaded: {len(world.vendors)} vendors, {len(world.purchase_orders)} POs, "
          f"{len(world.receipts)} goods receipts, {len(world.budgets)} budget lines")
    print(f"Wrote {len(world.invoices)} invoice PDFs to {s.inbox_dir}")


def cmd_run(s: Settings, conn, integrations: str = "mock", as_json: bool = False):
    extractor, source, jira, sheet = build_components(s, conn, integrations, load_arrival_times(s))
    summary = Pipeline(s, conn, extractor, source, jira, sheet).run()
    summary.paid = payment_run(conn, s.as_of.isoformat())
    if as_json:
        json.dump([asdict(o) for o in summary.outcomes], sys.stdout, indent=2, default=str)
        print()
    else:
        print(f"Processed {summary.files_new} new invoices ({summary.files_seen} in inbox): "
              f"{summary.matched} approved for payment, {summary.exceptions} sent to Jira. "
              f"Payment run paid {summary.paid} invoices that reached their due date.")
    return jira


def cmd_sla(s: Settings, conn, jira=None) -> None:
    if jira is None:
        _, _, jira, _ = build_components(s, conn)
    keys = run_sla_job(conn, jira, s)
    print(f"SLA job: {len(keys)} tickets open more than {s.sla_days} days got a reminder")


def cmd_report(s: Settings, conn) -> dict:
    metrics = evaluate(conn, s.manifest_path, s.extractor)
    kpis = dict(conn.execute("SELECT * FROM v_kpis").fetchone())
    export_bi(conn, s.output_dir)
    (s.output_dir / "run_report.json").write_text(json.dumps({"metrics": metrics, "kpis": kpis}, indent=2))
    (s.output_dir / "run_report.md").write_text(report_markdown(metrics, kpis))
    dash = build_dashboard(conn, s.output_dir / "dashboard.html", metrics)
    print(f"Dashboard: {dash}")
    print(f"Report:    {s.output_dir / 'run_report.md'}")
    return metrics


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ap_automation", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="setup + run + SLA job + report, offline")
    sub.add_parser("setup", help="generate synthetic ERP data and invoice PDFs")
    r = sub.add_parser("run", help="process new invoices in the inbox")
    r.add_argument("--json", action="store_true", help="print per-invoice outcomes as JSON (for n8n/Workato)")
    r.add_argument("--integrations", choices=["mock", "none"], default="mock",
                   help="'none' leaves Jira/Sheets to the orchestrator")
    sub.add_parser("sla", help="send reminders on exception tickets past the SLA")
    sub.add_parser("report", help="evaluate against ground truth, export BI CSVs, build dashboard")
    args = p.parse_args(argv)

    s = Settings()
    s.ensure_dirs()
    conn = db.connect(s.db_path)
    db.init_schema(conn)

    if args.cmd == "setup":
        cmd_setup(s, conn)
    elif args.cmd == "run":
        cmd_run(s, conn, args.integrations, args.json)
    elif args.cmd == "sla":
        cmd_sla(s, conn)
    elif args.cmd == "report":
        cmd_report(s, conn)
    elif args.cmd == "demo":
        print(f"== AP invoice automation demo (extractor: {s.extractor}, as of {s.as_of}) ==")
        cmd_setup(s, conn)
        jira = cmd_run(s, conn)
        cmd_sla(s, conn, jira)
        m = cmd_report(s, conn)
        print()
        print(f"Matched {m['touchless_rate']:.1%} of {m['invoices']} invoices with no manual work "
              f"({m['auto_approved']} auto-approved, {m['exceptions']} exceptions).")
        print(f"Caught {m['problems_caught_with_right_code']}/{m['problem_invoices']} planted problems with the "
              f"right code; false approvals: {len(m['false_approvals'])}; "
              f"overall accuracy {m['classification_accuracy']:.1%}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
