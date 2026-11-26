"""Synthetic company data: a small, realistic mini-ERP for a made-up company.

Produces departments, budgets, vendors, purchase orders, goods receipts and an
"invoice plan": the list of vendor invoices that will be rendered to PDF,
including the tricky cases. The plan doubles as ground truth for testing,
because every invoice carries the exception codes the pipeline *should* raise.

Everything is driven by a seeded RNG, so the same seed gives the same company,
the same PDFs and the same match-rate numbers.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------- #
# Static reference data
# --------------------------------------------------------------------------- #
DEPARTMENTS = [
    # dept_id, name, cost center, jira component, AP owner (ticket assignee)
    ("ENG", "Engineering", "CC-1100", "AP-Engineering", "priya.raman"),
    ("OPS", "Operations", "CC-1200", "AP-Operations", "marcus.lee"),
    ("MKT", "Marketing", "CC-1300", "AP-Marketing", "sofia.alvarez"),
    ("FAC", "Facilities", "CC-1400", "AP-Facilities", "daniel.okafor"),
    ("FIN", "Finance", "CC-1500", "AP-Finance", "hannah.kim"),
    ("PPL", "People", "CC-1600", "AP-People", "omar.haddad"),
    ("IT", "IT", "CC-1700", "AP-IT", "grace.chen"),
]

# category -> (departments that buy it, taxable, catalog of (sku suffix, description, low, high price))
CATALOG = {
    "office": (["FAC", "PPL", "FIN"], True, [
        ("PAP-500", "Copy paper, 10-ream case", 38, 52),
        ("TON-26A", "Laser toner cartridge", 85, 140),
        ("CHR-ERG", "Ergonomic task chair", 240, 420),
        ("DSK-STD", "Sit-stand desk frame", 380, 690),
        ("NTB-A5", "Notebook, A5 dotted (box of 24)", 60, 96),
    ]),
    "it_hardware": (["IT", "ENG"], True, [
        ("LPT-14P", "14in laptop, 32GB RAM", 1450, 2300),
        ("MON-27Q", "27in QHD monitor", 260, 410),
        ("DCK-USBC", "USB-C docking station", 170, 260),
        ("KBM-WL", "Wireless keyboard and mouse set", 55, 95),
        ("SWT-48P", "48-port PoE network switch", 1900, 3200),
    ]),
    "software": (["ENG", "IT", "MKT"], False, [
        ("LIC-OBS", "Observability platform, annual seat", 420, 780),
        ("LIC-DSN", "Design suite license, annual", 540, 720),
        ("LIC-CI", "CI build minutes pack (100k)", 900, 1500),
        ("SUP-PRM", "Premium support plan, quarterly", 2500, 6000),
    ]),
    "services": (["FIN", "PPL", "OPS", "MKT"], False, [
        ("SVC-HR", "Consulting hours", 150, 275),
        ("SVC-AUD", "Audit readiness workshop (day)", 1800, 3200),
        ("SVC-TRN", "Instructor-led training seat", 450, 900),
        ("SVC-CRE", "Creative production hours", 120, 210),
    ]),
    "facilities": (["FAC", "OPS"], False, [
        ("FMS-CLN", "Office cleaning, weekly visit", 380, 620),
        ("FMS-HVC", "HVAC preventive maintenance", 650, 1400),
        ("FMS-SEC", "Security guard shift (8h)", 310, 450),
        ("FMS-ELC", "Electrical repair callout", 280, 520),
    ]),
    "logistics": (["OPS"], True, [
        ("LOG-PLT", "Pallet freight, regional", 210, 380),
        ("LOG-CRT", "Courier same-day delivery", 45, 90),
        ("LOG-PKG", "Shipping cartons (bundle of 50)", 70, 115),
    ]),
    "marketing": (["MKT"], True, [
        ("MKT-PRT", "Printed brochures (1,000)", 480, 860),
        ("MKT-SWG", "Branded swag kit", 18, 34),
        ("MKT-BNR", "Trade show banner stand", 220, 390),
    ]),
    "catering": (["PPL", "MKT", "OPS"], True, [
        ("CAT-LCH", "Team lunch, per head", 18, 32),
        ("CAT-EVT", "Event catering package", 1200, 2600),
    ]),
}

# vendor master: (name on master, legal suffix printed on invoice, category, terms, status)
VENDORS = [
    ("Cobalt Office Supply", "LLC", "office", 30, "APPROVED"),
    ("Orchard Ergonomics", "Inc.", "office", 45, "APPROVED"),
    ("Brightline IT Hardware", "Inc.", "it_hardware", 30, "APPROVED"),
    ("Sable Networks", "Inc.", "it_hardware", 45, "APPROVED"),
    ("Juniper Cloud Software", "Ltd.", "software", 30, "APPROVED"),
    ("Kestrel Data Labs", "Inc.", "software", 60, "APPROVED"),
    ("Granite Peak Consulting", "LLC", "services", 30, "APPROVED"),
    ("Clearwater HR Solutions", "LLC", "services", 45, "APPROVED"),
    ("Pinecrest Training Institute", "", "services", 30, "APPROVED"),
    ("Harborview Facilities Services", "Co.", "facilities", 30, "APPROVED"),
    ("Tidewater Electrical Supply", "Inc.", "facilities", 30, "APPROVED"),
    ("Silverline Security Services", "LLC", "facilities", 45, "APPROVED"),
    ("Polar Logistics", "Co.", "logistics", 30, "APPROVED"),
    ("Atlas Freight Partners", "LLC", "logistics", 30, "APPROVED"),
    ("Lumen Marketing Studio", "", "marketing", 30, "APPROVED"),
    ("Redwood Print & Packaging", "Inc.", "marketing", 30, "APPROVED"),
    ("Meridian Catering Group", "LLC", "catering", 15, "APPROVED"),
    ("Quarry Road Provisions", "Inc.", "catering", 15, "APPROVED"),
    ("Vantage Telecom Services", "Inc.", "software", 30, "APPROVED"),
    # Not approved for payment: blocked after a compliance review, or onboarding incomplete.
    ("Fairhaven Promotions", "LLC", "marketing", 30, "BLOCKED"),
    ("Ironwood Tech Resale", "Inc.", "it_hardware", 30, "PENDING"),
    ("Bayside Temp Staffing", "LLC", "services", 30, "BLOCKED"),
]

# Exception scenarios and the codes the pipeline must raise for each.
SCENARIO_CODES = {
    "clean": [],
    "within_tolerance": [],                     # small price variance, still auto-approved
    "missing_po": ["MISSING_PO"],
    "unknown_po": ["PO_NOT_FOUND"],
    "duplicate": ["DUPLICATE_INVOICE"],
    "amount_variance": ["AMOUNT_OUT_OF_TOLERANCE"],
    "quantity_mismatch": ["QUANTITY_MISMATCH"],
    "unapproved_vendor": ["VENDOR_NOT_APPROVED"],
    "poor_scan": ["EXTRACTION_FAILED"],         # blurry image-only PDF
    "ocr_noise": ["EXTRACTION_FAILED"],         # misread digit -> totals don't reconcile
}

# How many of each tricky case to plant (the rest are clean).
SCENARIO_COUNTS = {
    "within_tolerance": 10,
    "missing_po": 6,
    "unknown_po": 3,
    "duplicate": 6,
    "amount_variance": 8,
    "quantity_mismatch": 6,
    "unapproved_vendor": 5,
    "poor_scan": 4,
    "ocr_noise": 3,
}

TAX_RATE = 0.08625


def fiscal_quarter(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


@dataclass
class InvoiceSpec:
    file_name: str
    scenario: str
    expected_codes: list[str]
    vendor_id: str
    vendor_display_name: str
    invoice_number: str
    po_number: str | None          # the real PO this invoice relates to
    printed_po: str | None         # what is printed on the PDF
    invoice_date: str
    due_date: str
    received_at: str
    template: int
    lines: list[dict]
    subtotal: float
    tax: float
    total: float
    render: str = "text"           # text | poor_scan | ocr_noise
    dept_id: str = ""
    duplicate_of: str | None = None
    noise: dict = field(default_factory=dict)


@dataclass
class World:
    departments: list[tuple]
    vendors: list[dict]
    purchase_orders: list[dict]
    po_lines: list[dict]
    receipts: list[dict]
    receipt_lines: list[dict]
    budgets: list[dict]
    invoices: list[InvoiceSpec]


def _r2(x: float) -> float:
    return round(x + 1e-9, 2)


def generate_world(seed: int = 42, n_invoices: int = 150, as_of: date = date(2026, 11, 20)) -> World:
    rng = random.Random(seed)

    vendors = []
    for i, (name, suffix, cat, terms, status) in enumerate(VENDORS, start=1):
        slug = "".join(w[0] for w in name.replace("&", "").split())[:3].upper()
        vendors.append({
            "vendor_id": f"V{100 + i}",
            "name": name,
            "display_name": f"{name} {suffix}".strip(),
            "status": status,
            "category": cat,
            "payment_terms_days": terms,
            "remit_email": f"billing@{name.lower().replace(' ', '').replace('&', '')[:18]}.example",
            "slug": slug,
            "inv_seq": rng.randint(1000, 8000),
        })
    approved = [v for v in vendors if v["status"] == "APPROVED"]
    unapproved = [v for v in vendors if v["status"] != "APPROVED"]

    # ---- scenario list ------------------------------------------------------
    counts = dict(SCENARIO_COUNTS)
    n_tricky = sum(counts.values())
    if n_invoices < n_tricky + 10:
        raise ValueError(f"n_invoices must be at least {n_tricky + 10}")
    counts["clean"] = n_invoices - n_tricky
    n_dup = counts.pop("duplicate")
    scenarios = [s for s, c in counts.items() for _ in range(c)]
    rng.shuffle(scenarios)

    po_seq = 10000
    purchase_orders, po_lines, receipts, receipt_lines = [], [], [], []
    specs: list[InvoiceSpec] = []

    def new_po(vendor: dict, order_date: date, received: bool = True, short_ship: bool = False):
        nonlocal po_seq
        po_seq += rng.randint(1, 4)
        po_number = f"PO-{po_seq}"
        depts, _, items = CATALOG[vendor["category"]]
        dept = rng.choice(depts)
        purchase_orders.append({
            "po_number": po_number, "vendor_id": vendor["vendor_id"], "dept_id": dept,
            "order_date": order_date.isoformat(), "status": "OPEN", "currency": "USD",
        })
        chosen = rng.sample(items, k=min(len(items), rng.randint(1, 4)))
        lines = []
        for ln, (sku, desc, lo, hi) in enumerate(chosen, start=1):
            if hi > 1000:
                qty = rng.randint(1, 4)
            elif hi > 200:
                qty = rng.randint(1, 12)
            else:
                qty = rng.choice([5, 10, 12, 20, 25, 40, 50])
            price = _r2(rng.uniform(lo, hi))
            line = {"po_number": po_number, "line_no": ln, "sku": f"{vendor['slug']}-{sku}",
                    "description": desc, "quantity": float(qty), "unit_price": price}
            po_lines.append(line)
            lines.append(line)
        if received:
            rid = f"GR-{po_seq}"
            rdate = order_date + timedelta(days=rng.randint(3, 9))
            receipts.append({"receipt_id": rid, "po_number": po_number, "receipt_date": rdate.isoformat()})
            short_line = rng.randrange(len(lines)) if short_ship else None
            for i, line in enumerate(lines):
                q = line["quantity"]
                if i == short_line:
                    q = max(0.0, q - max(1, int(q * rng.uniform(0.2, 0.5))))
                receipt_lines.append({"receipt_id": rid, "po_number": po_number,
                                      "line_no": line["line_no"], "quantity_received": q})
        return po_number, dept, lines

    def inv_number(vendor: dict, template: int) -> str:
        vendor["inv_seq"] += rng.randint(1, 17)
        seq = vendor["inv_seq"]
        if template == 0:
            return f"INV-{seq:06d}"
        if template == 1:
            return f"{vendor['slug']}-2026-{seq:04d}"
        return f"{seq}"

    for idx, scenario in enumerate(scenarios):
        vendor = rng.choice(unapproved if scenario == "unapproved_vendor" else approved)
        inv_date = as_of - timedelta(days=rng.randint(2, 110))
        order_date = inv_date - timedelta(days=rng.randint(12, 30))
        po_number, dept, lines = new_po(vendor, order_date, short_ship=(scenario == "quantity_mismatch"))

        inv_lines = []
        bump_line = rng.randrange(len(lines))
        for line in lines:
            price = line["unit_price"]
            if scenario == "amount_variance":
                price = _r2(price * rng.uniform(1.05, 1.15))
            inv_lines.append({"line_no": line["line_no"], "sku": line["sku"], "description": line["description"],
                              "quantity": line["quantity"], "unit_price": price})
        if scenario == "within_tolerance":
            # nudge one line so the whole invoice lands 0.3%-1.6% over PO value
            po_value = sum(l["quantity"] * l["unit_price"] for l in lines)
            target = po_value * rng.uniform(0.003, 0.016)
            l = inv_lines[bump_line]
            l["unit_price"] = _r2(l["unit_price"] + target / l["quantity"])
        for l in inv_lines:
            l["amount"] = _r2(l["quantity"] * l["unit_price"])

        subtotal = _r2(sum(l["amount"] for l in inv_lines))
        taxable = CATALOG[vendor["category"]][1]
        tax = _r2(subtotal * TAX_RATE) if taxable else 0.0
        total = _r2(subtotal + tax)
        template = rng.randrange(3)

        printed_po = po_number
        if scenario == "missing_po":
            printed_po = None
        elif scenario == "unknown_po":
            printed_po = f"PO-{rng.randint(60000, 69999)}"  # typo / PO from another entity

        received = datetime.combine(inv_date + timedelta(days=rng.randint(0, 3)), datetime.min.time()) \
            + timedelta(hours=rng.randint(7, 18), minutes=rng.randint(0, 59))
        received = min(received, datetime.combine(as_of, datetime.min.time()) + timedelta(hours=6))
        number = inv_number(vendor, template)
        render = scenario if scenario in ("poor_scan", "ocr_noise") else "text"
        noise = {}
        if scenario == "ocr_noise":
            noise = {"line_index": rng.randrange(len(inv_lines))}

        specs.append(InvoiceSpec(
            file_name="", scenario=scenario, expected_codes=list(SCENARIO_CODES[scenario]),
            vendor_id=vendor["vendor_id"], vendor_display_name=vendor["display_name"],
            invoice_number=number, po_number=po_number, printed_po=printed_po,
            invoice_date=inv_date.isoformat(),
            due_date=(inv_date + timedelta(days=vendor["payment_terms_days"])).isoformat(),
            received_at=received.isoformat(timespec="seconds"), template=template, lines=inv_lines,
            subtotal=subtotal, tax=tax, total=total, render=render, dept_id=dept, noise=noise,
        ))

    # ---- duplicates: a vendor re-sends an invoice that was already processed --
    clean_specs = [s for s in specs if s.scenario == "clean"
                   and date.fromisoformat(s.invoice_date) < as_of - timedelta(days=25)]
    for orig in rng.sample(clean_specs, k=n_dup):
        resent = datetime.fromisoformat(orig.received_at) + timedelta(days=rng.randint(6, 20), hours=rng.randint(1, 5))
        dup = InvoiceSpec(**{**asdict(orig)})
        dup.scenario = "duplicate"
        dup.expected_codes = list(SCENARIO_CODES["duplicate"])
        dup.received_at = resent.isoformat(timespec="seconds")
        dup.template = orig.template
        dup.duplicate_of = orig.invoice_number
        specs.append(dup)

    # ---- open POs not yet invoiced (commitments) ------------------------------
    for _ in range(max(10, n_invoices // 8)):
        vendor = rng.choice(approved)
        new_po(vendor, as_of - timedelta(days=rng.randint(1, 40)), received=rng.random() < 0.5)

    # ---- order by arrival and assign file names --------------------------------
    specs.sort(key=lambda s: s.received_at)
    for i, s in enumerate(specs, start=1):
        safe_vendor = s.vendor_display_name.split()[0].lower()
        s.file_name = f"{i:04d}_{safe_vendor}_{s.invoice_number.replace('/', '-')}.pdf"

    # ---- budgets: planned spend per dept/quarter, with some over / under -------
    # Planned spend = what each department is expected to be invoiced in the quarter
    # (original invoices only) plus open PO commitments.
    po_value: dict[str, float] = {}
    for l in po_lines:
        po_value[l["po_number"]] = po_value.get(l["po_number"], 0) + l["quantity"] * l["unit_price"]
    spend: dict[tuple, float] = {}
    invoiced_pos = set()
    for s in specs:
        if s.scenario == "duplicate":
            continue
        invoiced_pos.add(s.po_number)
        key = (s.dept_id, fiscal_quarter(date.fromisoformat(s.invoice_date)))
        spend[key] = spend.get(key, 0) + s.subtotal
    for po in purchase_orders:
        if po["po_number"] in invoiced_pos:
            continue
        key = (po["dept_id"], fiscal_quarter(date.fromisoformat(po["order_date"])))
        spend[key] = spend.get(key, 0) + po_value[po["po_number"]]
    quarters = sorted({q for _, q in spend} | {fiscal_quarter(as_of)})
    budgets = []
    for dept_id, *_ in DEPARTMENTS:
        for q in quarters:
            base = spend.get((dept_id, q), 0)
            amt = max(5000, round(base * rng.uniform(0.85, 1.2) / 1000) * 1000)
            budgets.append({"dept_id": dept_id, "fiscal_quarter": q, "amount": float(amt)})

    return World(DEPARTMENTS, vendors, purchase_orders, po_lines, receipts, receipt_lines, budgets, specs)


def load_erp(conn: sqlite3.Connection, world: World) -> None:
    """Write master data, POs and receipts into the mini-ERP tables."""
    for t in ("receipt_lines", "goods_receipts", "po_lines", "purchase_orders", "budgets", "vendors", "departments"):
        conn.execute(f"DELETE FROM {t}")
    conn.executemany("INSERT INTO departments VALUES (?,?,?,?,?)", world.departments)
    conn.executemany(
        "INSERT INTO vendors VALUES (:vendor_id,:name,:status,:category,:payment_terms_days,:remit_email)",
        world.vendors)
    conn.executemany("INSERT INTO budgets VALUES (:dept_id,:fiscal_quarter,:amount)", world.budgets)
    conn.executemany(
        "INSERT INTO purchase_orders VALUES (:po_number,:vendor_id,:dept_id,:order_date,:status,:currency)",
        world.purchase_orders)
    conn.executemany(
        "INSERT INTO po_lines VALUES (:po_number,:line_no,:sku,:description,:quantity,:unit_price)", world.po_lines)
    conn.executemany("INSERT INTO goods_receipts VALUES (:receipt_id,:po_number,:receipt_date)", world.receipts)
    conn.executemany(
        "INSERT INTO receipt_lines VALUES (:receipt_id,:po_number,:line_no,:quantity_received)", world.receipt_lines)
    conn.commit()
