"""Static HTML dashboard over the reporting views.

One self-contained file (no CDN, no server): KPI tiles, unpaid invoices by
age, spend by vendor and department, budget vs actual, exception rate,
processing time and the open exception queue. Every chart has a table view.
For Looker Studio / Power BI, point the tool at the same views (or the CSVs
in outputs/bi/) - see docs/dashboard.md.
"""

from __future__ import annotations

import html
import sqlite3
from pathlib import Path

CSS = """
:root{color-scheme:light;--page:#f9f9f9;--surface:#ffffff;--ink:#191919;--ink-2:#666666;--grid:#e7e7e7;
--axis:#c2c2c2;--border:rgba(11,11,11,.10);--s1:#257de0;--s2:#f47230;--good:#0aac23;--good-ink:#006c10;
--critical:#dc3c3d;--warning:#fbb315;--track:#eef3fb}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0b0b0b;
--surface:#191919;--ink:#efefef;--ink-2:#9c9c9c;--grid:#303030;--axis:#474747;--border:rgba(255,255,255,.10);
--s1:#338ced;--s2:#e76624;--good:#0aac23;--good-ink:#53c556;--critical:#dc3c3d;--track:#1f2a38}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0b0b0b;--surface:#191919;--ink:#efefef;--ink-2:#9c9c9c;
--grid:#303030;--axis:#474747;--border:rgba(255,255,255,.10);--s1:#338ced;--s2:#e76624;--good:#0aac23;
--good-ink:#53c556;--critical:#dc3c3d;--track:#1f2a38}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:0 0 2px}
.sub{color:var(--ink-2);margin:0 0 20px}
.cap{color:var(--ink-2);font-size:12px;margin:0 0 12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.tile,.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile .k{color:var(--ink-2);font-size:12px}.tile .v{font-size:26px;font-weight:600;margin-top:2px}
.tile .n{color:var(--ink-2);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr));gap:16px}
.card{min-width:0}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--ink-2);font-size:12px} svg .val{fill:var(--ink);font-variant-numeric:tabular-nums}
.bar{transition:opacity .1s} .bar:hover{opacity:.8}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--ink-2);font-size:12px;margin:0 0 8px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
details{margin-top:10px} summary{cursor:pointer;color:var(--ink-2);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;border:1px solid var(--border)}
.over{color:var(--critical);font-weight:600}.ok{color:var(--good-ink)}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);padding:6px 9px;border-radius:6px;
font-size:12px;opacity:0;transition:opacity .08s;max-width:260px;z-index:9}
footer{color:var(--ink-2);font-size:12px;margin-top:24px}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el=>{
 el.addEventListener('mousemove',e=>{tip.textContent=el.dataset.tip;tip.style.opacity=1;
  const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8);tip.style.left=x+'px';tip.style.top=(e.clientY+14)+'px'});
 el.addEventListener('mouseleave',()=>tip.style.opacity=0);});
"""


def _money(x, short=False) -> str:
    if x is None:
        return "-"
    if short and abs(x) >= 1000:
        return f"${x / 1000:,.1f}k"
    return f"${x:,.0f}"


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _bar_path(x0: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Horizontal bar anchored at x0 with a rounded data end."""
    r = min(r, w / 2, h / 2)
    if w <= 0:
        return ""
    return (f"M{x0:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}"
            f"a{r},{r} 0 0 1 {-r},{r}h{-(w - r):.1f}z")


def hbar(rows: list[tuple[str, float, str]], color: str = "var(--s1)", width: int = 520, fmt=None) -> str:
    fmt = fmt or (lambda v: _money(v, True))
    """rows: (label, value, tooltip)."""
    if not rows:
        return "<p class='cap'>No data.</p>"
    lab_w, val_w, bh, gap = 170, 70, 16, 10
    plot_w = width - lab_w - val_w
    vmax = max(v for _, v, _ in rows) or 1
    h = len(rows) * (bh + gap)
    out = [f"<svg viewBox='0 0 {width} {h}' role='img'>"]
    out.append(f"<line x1='{lab_w}' x2='{lab_w}' y1='0' y2='{h - gap}' stroke='var(--axis)'/>")
    for i, (label, v, tip) in enumerate(rows):
        y = i * (bh + gap)
        w = max(1.5, plot_w * v / vmax)
        lab = label if len(label) <= 24 else label[:23] + "…"
        out.append(f"<text x='{lab_w - 8}' y='{y + bh - 4}' text-anchor='end'>{_e(lab)}</text>")
        out.append(f"<path class='bar' d='{_bar_path(lab_w, y, w, bh)}' style='fill:{color}' data-tip='{_e(tip)}'/>")
        out.append(f"<rect x='{lab_w}' y='{y - 4}' width='{plot_w + val_w}' height='{bh + 8}' fill='transparent' "
                   f"data-tip='{_e(tip)}'/>")
        out.append(f"<text class='val' x='{lab_w + w + 6}' y='{y + bh - 4}'>{fmt(v)}</text>")
    out.append("</svg>")
    return "".join(out)


def stacked_aging(buckets: list[str], data: dict, width: int = 520) -> str:
    """Horizontal stacked bars: approved-awaiting-payment vs exception, per age bucket."""
    lab_w, val_w, bh, gap = 90, 80, 20, 14
    plot_w = width - lab_w - val_w
    totals = {b: sum(data.get((b, s), (0, 0))[1] for s in ("APPROVED", "EXCEPTION")) for b in buckets}
    vmax = max(totals.values() or [1]) or 1
    h = len(buckets) * (bh + gap)
    out = [f"<svg viewBox='0 0 {width} {h}' role='img'>",
           f"<line x1='{lab_w}' x2='{lab_w}' y1='0' y2='{h - gap}' stroke='var(--axis)'/>"]
    for i, b in enumerate(buckets):
        y = i * (bh + gap)
        x = lab_w
        out.append(f"<text x='{lab_w - 8}' y='{y + bh - 6}' text-anchor='end'>{_e(b)}</text>")
        segs = [(s, c) for s, c in (("APPROVED", "var(--s1)"), ("EXCEPTION", "var(--s2)")) if data.get((b, s))]
        for j, (s, color) in enumerate(segs):
            n, amt = data[(b, s)]
            w = plot_w * amt / vmax
            label = "Approved, awaiting payment" if s == "APPROVED" else "Exception (on hold)"
            tip = f"{b} - {label}: {n} invoices, {_money(amt)}"
            last = j == len(segs) - 1
            seg_w = max(0.0, w - (0 if last else 2))  # 2px surface gap between segments
            if last:
                out.append(f"<path class='bar' d='{_bar_path(x, y, seg_w, bh)}' style='fill:{color}' "
                           f"data-tip='{_e(tip)}'/>")
            else:
                out.append(f"<rect class='bar' x='{x:.1f}' y='{y}' width='{seg_w:.1f}' height='{bh}' "
                           f"style='fill:{color}' data-tip='{_e(tip)}'/>")
            x += w
        out.append(f"<text class='val' x='{x + 6:.1f}' y='{y + bh - 6}'>{_money(totals[b], True)}</text>")
    out.append("</svg>")
    return "".join(out)


def bullet(rows: list[dict], width: int = 520) -> str:
    """Budget vs actual: track = budget, bar = actual, tick = budget line."""
    lab_w, val_w, bh, gap = 110, 120, 14, 16
    plot_w = width - lab_w - val_w
    vmax = max(max(r["budget"], r["actual"]) for r in rows) or 1
    h = len(rows) * (bh + gap)
    out = [f"<svg viewBox='0 0 {width} {h}' role='img'>"]
    for i, r in enumerate(rows):
        y = i * (bh + gap)
        bw = plot_w * r["budget"] / vmax
        aw = max(1.5, plot_w * r["actual"] / vmax)
        over = r["actual"] > r["budget"]
        tip = (f"{r['department']} {r['fiscal_quarter']}: actual {_money(r['actual'])} of budget "
               f"{_money(r['budget'])} ({r['pct_used']:.0%}); open commitments {_money(r['committed'])}")
        out.append(f"<text x='{lab_w - 8}' y='{y + bh - 2}' text-anchor='end'>{_e(r['department'])}</text>")
        out.append(f"<rect x='{lab_w}' y='{y - 3}' width='{bw:.1f}' height='{bh + 6}' rx='3' style='fill:var(--track)' "
                   f"data-tip='{_e(tip)}'/>")
        out.append(f"<path class='bar' d='{_bar_path(lab_w, y, aw, bh)}' style='fill:var(--s1)' data-tip='{_e(tip)}'/>")
        out.append(f"<line x1='{lab_w + bw:.1f}' x2='{lab_w + bw:.1f}' y1='{y - 5}' y2='{y + bh + 5}' "
                   f"style='stroke:var(--ink)' stroke-width='2'/>")
        status = f"▲ {r['pct_used']:.0%}" if over else f"{r['pct_used']:.0%}"
        cls = "over" if over else ""
        out.append(f"<text class='val {cls}' x='{lab_w + max(aw, bw) + 8:.1f}' y='{y + bh - 2}'"
                   f"{' style=\"fill:var(--critical);font-weight:600\"' if over else ''}>{status}</text>")
    out.append("</svg>")
    return "".join(out)


def table(headers: list[str], rows: list[list], num_cols: set[int] = frozenset()) -> str:
    th = "".join(f"<th class='{'num' if i in num_cols else ''}'>{_e(h)}</th>" for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f"<td class='{'num' if i in num_cols else ''}'>{_e(c)}</td>"
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<div class='scroll'><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"


def card(title: str, caption: str, body: str, tbl: str = "", legend: str = "") -> str:
    det = f"<details><summary>Show table</summary>{tbl}</details>" if tbl else ""
    return f"<section class='card'><h2>{_e(title)}</h2><p class='cap'>{caption}</p>{legend}{body}{det}</section>"


def build_dashboard(conn: sqlite3.Connection, path: Path, metrics: dict | None = None) -> Path:
    q = lambda sql: [dict(r) for r in conn.execute(sql).fetchall()]  # noqa: E731
    k = q("SELECT * FROM v_kpis")[0]
    as_of = conn.execute("SELECT value FROM app_settings WHERE key='as_of'").fetchone()[0]
    m = metrics or {}

    tiles = [
        ("Invoices processed", f"{k['invoices_processed']}", "replayed through the pipeline"),
        ("Touchless rate", f"{(k['touchless_rate'] or 0):.1%}", f"{k['auto_approved']} approved with no manual work"),
        ("Exceptions to Jira", f"{k['exceptions']}", "routed by department"),
        ("Problems caught", f"{m.get('problems_caught_with_right_code', '-')}/{m.get('problem_invoices', '-')}",
         f"false approvals: {len(m.get('false_approvals', [])) if m else '-'}"),
        ("Avg processing time", f"{(k['avg_processing_ms'] or 0):.0f} ms", "extract + validate + match + route"),
        ("Past 3-day SLA", f"{k['tickets_past_sla']}", f"of {k['open_tickets']} open tickets"),
        ("Unpaid", _money(k["unpaid_amount"], True), "approved not yet due + on hold"),
    ]
    tiles_html = "".join(f"<div class='tile'><div class='k'>{_e(a)}</div><div class='v'>{_e(b)}</div>"
                         f"<div class='n'>{_e(c)}</div></div>" for a, b, c in tiles)

    # aging
    buckets = ["0-30 days", "31-60 days", "61-90 days", "90+ days", "Unknown date"]
    aging_rows = q("SELECT * FROM v_unpaid_aging")
    aging = {(r["age_bucket"], r["status"]): (r["invoice_count"], r["amount"]) for r in aging_rows}
    buckets = [b for b in buckets if any(key[0] == b for key in aging)]
    legend = ("<div class='legend'><span><i class='sw' style='background:var(--s1)'></i>Approved, awaiting payment"
              "</span><span><i class='sw' style='background:var(--s2)'></i>Exception (on hold)</span></div>")
    aging_tbl = table(["Age", "Status", "Invoices", "Amount"],
                      [[b, s.title(), aging[(b, s)][0], _money(aging[(b, s)][1])]
                       for b in buckets for s in ("APPROVED", "EXCEPTION") if (b, s) in aging], {2, 3})

    vendors = q("SELECT * FROM v_spend_by_vendor LIMIT 10")
    vendor_svg = hbar([(r["vendor_name"], r["spend"], f"{r['vendor_name']}: {_money(r['spend'])} across "
                        f"{r['invoice_count']} invoices") for r in vendors])
    vendor_tbl = table(["Vendor", "Invoices", "Spend"],
                       [[r["vendor_name"], r["invoice_count"], _money(r["spend"])] for r in vendors], {1, 2})

    depts = q("SELECT * FROM v_spend_by_department")
    dept_svg = hbar([(r["department"], r["spend"], f"{r['department']}: {_money(r['spend'])} across "
                      f"{r['invoice_count']} invoices") for r in depts])
    dept_tbl = table(["Department", "Invoices", "Spend"],
                     [[r["department"], r["invoice_count"], _money(r["spend"])] for r in depts], {1, 2})

    bva = q("SELECT * FROM v_budget_vs_actual")
    quarters = sorted({r["fiscal_quarter"] for r in bva})
    bva_cards = []
    for qtr in quarters[-2:]:
        rows = [r for r in bva if r["fiscal_quarter"] == qtr]
        tbl = table(["Department", "Budget", "Actual", "Committed (open POs)", "Remaining", "% used"],
                    [[r["department"], _money(r["budget"]), _money(r["actual"]), _money(r["committed"]),
                      _money(r["remaining"]), f"{r['pct_used']:.0%}"] for r in rows], {1, 2, 3, 4, 5})
        bva_cards.append(card(f"Budget vs actual, {qtr}",
                              "Bar = approved and paid invoices (pre-tax). Shaded track and black tick = budget. "
                              "▲ marks a department over budget.", bullet(rows), tbl))

    exc = q("SELECT * FROM v_exception_summary")
    exc_svg = hbar([(r["exception_code"].replace("_", " ").title().replace("Po ", "PO "), r["invoice_count"],
                     f"{r['exception_code']}: {r['invoice_count']} invoices ({r['share_of_all_invoices']:.1%} of all)")
                    for r in exc], color="var(--s2)", fmt=lambda v: f"{v:.0f}")
    exc_tbl = table(["Exception", "Invoices", "Share of all invoices"],
                    [[r["exception_code"], r["invoice_count"], f"{r['share_of_all_invoices']:.1%}"] for r in exc],
                    {1, 2})

    pt = q("SELECT * FROM v_processing_time")
    pt_tbl = table(["Path", "Invoices", "Avg ms", "Max ms", "Avg open ticket age (days)"],
                   [[r["path"], r["invoices"], r["avg_processing_ms"], r["max_processing_ms"],
                     r["avg_open_ticket_age_days"] if r["avg_open_ticket_age_days"] is not None else "-"]
                    for r in pt], {1, 2, 3, 4})
    pt_body = "<div class='tiles' style='margin:0'>" + "".join(
        f"<div class='tile'><div class='k'>{_e(r['path'])}</div><div class='v'>{r['avg_processing_ms']:.0f} ms</div>"
        f"<div class='n'>{r['invoices']} invoices"
        + (f" · tickets open {r['avg_open_ticket_age_days']:.0f} days on average" if r["avg_open_ticket_age_days"]
           else " · approved same run") + "</div></div>" for r in pt) + "</div>"

    tickets = q("""SELECT t.ticket_key, t.priority, t.component, t.assignee, t.exception_codes, t.amount,
                          CAST(julianday((SELECT value FROM app_settings WHERE key='as_of')) - julianday(t.created_at)
                               AS INTEGER) AS age, t.last_reminder_at
                   FROM exception_tickets t WHERE t.status <> 'Done'
                   ORDER BY CASE t.priority WHEN 'Highest' THEN 0 WHEN 'High' THEN 1 ELSE 2 END, age DESC""")
    queue = table(["Ticket", "Priority", "Component", "Assignee", "Exception", "Amount", "Age (days)", "SLA"],
                  [[t["ticket_key"], t["priority"], t["component"], t["assignee"],
                    t["exception_codes"].replace(",", ", "), _money(t["amount"]), t["age"],
                    "Reminder sent" if t["last_reminder_at"] else "Within SLA"] for t in tickets], {5, 6})

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AP Automation Dashboard</title>
<style>{CSS}</style></head><body><main>
<h1>Accounts payable automation</h1>
<p class="sub">Procure-to-pay pipeline for Bluefin Mobility (synthetic data), as of {as_of}.
Source: SQLite reporting views (v_kpis, v_unpaid_aging, v_spend_by_*, v_budget_vs_actual, v_exception_summary,
v_processing_time).</p>
<div class="tiles">{tiles_html}</div>
<div class="grid">
{card("Unpaid invoices by age", "Days since invoice date. Exceptions on hold age fastest, which is what the "
      "3-day Jira reminder is for.", stacked_aging(buckets, aging), aging_tbl, legend)}
{card("Exception rate by type", f"{k['exceptions']} of {k['invoices_processed']} invoices "
      f"({1 - (k['touchless_rate'] or 0):.1%}) needed a person. An invoice can carry more than one code.",
      exc_svg, exc_tbl)}
{card("Spend by vendor, top 10", "Approved and paid invoices, including tax.", vendor_svg, vendor_tbl)}
{card("Spend by department", "Department comes from the PO the invoice matched.", dept_svg, dept_tbl)}
{"".join(bva_cards)}
{card("Processing time", "Measured wall-clock time per invoice in this run.", pt_body, pt_tbl)}
</div>
<section class="card" style="margin-top:16px"><h2>Open exception queue</h2>
<p class="cap">Jira tickets, highest priority first. Routing: component and assignee by department; Highest for
invoices of $10k or more; High for duplicates and unapproved vendors.</p>{queue}</section>
<footer>Generated by <code>python -m ap_automation report</code>. The same views feed Looker Studio or Power BI;
see docs/dashboard.md.</footer>
</main><div id="tip" role="tooltip"></div><script>{JS}</script></body></html>"""
    Path(path).write_text(page)
    return Path(path)
