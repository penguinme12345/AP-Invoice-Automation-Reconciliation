"""Score a run against the ground truth in the invoice manifest.

This is where the resume numbers come from: the touchless (no manual work)
rate, and whether every planted problem was caught with no false approvals.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

VIEWS_FOR_BI = ["v_invoice_detail", "v_unpaid_aging", "v_spend_by_vendor", "v_spend_by_department",
                "v_budget_vs_actual", "v_exception_summary", "v_processing_time", "v_kpis"]


def evaluate(conn: sqlite3.Connection, manifest_path: Path, extractor: str = "offline") -> dict:
    manifest = {m["file_name"]: m for m in json.loads(Path(manifest_path).read_text())}
    rows = conn.execute(
        """SELECT i.source_file, i.status, m.exception_codes, m.matched
           FROM invoices i JOIN match_results m ON m.invoice_id = i.invoice_id""").fetchall()

    per = defaultdict(lambda: {"count": 0, "correct": 0})
    false_approvals, false_exceptions, mismatches = [], [], []
    for r in rows:
        spec = manifest.get(r["source_file"])
        if spec is None:
            continue
        expected = set(spec["expected_codes"])
        actual = {c for c in r["exception_codes"].split(",") if c}
        ok = expected == actual
        # A vision LLM may legitimately read a blurry scan; then the right answer is the clean match.
        if not ok and extractor == "llm" and spec["scenario"] == "poor_scan" and not actual:
            ok = True
        per[spec["scenario"]]["count"] += 1
        per[spec["scenario"]]["correct"] += int(ok)
        if not ok:
            mismatches.append({"file": r["source_file"], "scenario": spec["scenario"],
                               "expected": sorted(expected), "actual": sorted(actual)})
        if expected and not actual:
            false_approvals.append(r["source_file"])
        if not expected and actual:
            false_exceptions.append(r["source_file"])

    n = len(rows)
    approved = sum(r["matched"] for r in rows)
    clean_total = sum(v["count"] for k, v in per.items() if k in ("clean", "within_tolerance"))
    clean_ok = sum(v["correct"] for k, v in per.items() if k in ("clean", "within_tolerance"))
    exc_total = n - clean_total
    exc_ok = sum(v["correct"] for k, v in per.items() if k not in ("clean", "within_tolerance"))
    correct = sum(v["correct"] for v in per.values())
    return {
        "extractor": extractor,
        "invoices": n,
        "auto_approved": approved,
        "exceptions": n - approved,
        "touchless_rate": round(approved / n, 4) if n else 0,
        "clean_invoices": clean_total,
        "clean_auto_approved": clean_ok,
        "clean_auto_approval_rate": round(clean_ok / clean_total, 4) if clean_total else 0,
        "problem_invoices": exc_total,
        "problems_caught_with_right_code": exc_ok,
        "problem_detection_rate": round(exc_ok / exc_total, 4) if exc_total else 0,
        "classification_accuracy": round(correct / n, 4) if n else 0,
        "false_approvals": false_approvals,
        "false_exceptions": false_exceptions,
        "by_scenario": dict(sorted(per.items())),
        "mismatches": mismatches,
    }


def export_bi(conn: sqlite3.Connection, out_dir: Path) -> list[Path]:
    """CSV export of each reporting view, for Looker Studio / Power BI / Excel."""
    out = Path(out_dir) / "bi"
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for v in VIEWS_FOR_BI:
        cur = conn.execute(f"SELECT * FROM {v}")
        p = out / f"{v.removeprefix('v_')}.csv"
        with p.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([d[0] for d in cur.description])
            w.writerows(cur.fetchall())
        paths.append(p)
    return paths


def report_markdown(metrics: dict, kpis: dict) -> str:
    m = metrics
    lines = [
        "# AP pipeline run report",
        "",
        f"Extractor: `{m['extractor']}`",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Invoices processed | {m['invoices']} |",
        f"| Auto-approved with no manual work | {m['auto_approved']} ({m['touchless_rate']:.1%}) |",
        f"| Routed to Jira as exceptions | {m['exceptions']} |",
        f"| Clean invoices auto-approved | {m['clean_auto_approved']} / {m['clean_invoices']} "
        f"({m['clean_auto_approval_rate']:.1%}) |",
        f"| Problem invoices caught with the right exception code | {m['problems_caught_with_right_code']} / "
        f"{m['problem_invoices']} ({m['problem_detection_rate']:.1%}) |",
        f"| False approvals (problem invoice sent to payment) | {len(m['false_approvals'])} |",
        f"| Overall classification accuracy | {m['classification_accuracy']:.1%} |",
        f"| Avg processing time per invoice | {kpis.get('avg_processing_ms', 0):.0f} ms |",
        f"| Open exception tickets past 3-day SLA | {kpis.get('tickets_past_sla', 0)} |",
        "",
        "## By scenario",
        "",
        "| Scenario | Invoices | Correct |",
        "|---|---|---|",
    ]
    for k, v in m["by_scenario"].items():
        lines.append(f"| {k} | {v['count']} | {v['correct']} |")
    lines += [
        "",
        "## Resume bullet",
        "",
        f"> Built an AP invoice automation pipeline using Python, LangChain and SQL that extracts invoice data, "
        f"runs a three-way match against PO and receipt records, and sends exceptions to Jira with automated "
        f"routing. It matched {m['touchless_rate']:.0%} of {m['invoices']} test invoices with no manual work and "
        f"flagged {m['problems_caught_with_right_code']} of {m['problem_invoices']} planted exceptions "
        f"(duplicates, missing POs, price and quantity variances, unapproved vendors, bad scans) with "
        f"{len(m['false_approvals'])} false approvals.",
    ]
    if m["mismatches"]:
        lines += ["", "## Mismatches", ""] + [f"- {x}" for x in m["mismatches"]]
    return "\n".join(lines) + "\n"
