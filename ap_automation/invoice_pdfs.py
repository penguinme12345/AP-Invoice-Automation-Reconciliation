"""Render the invoice plan to vendor-style PDF invoices.

Three layouts with different labels and date formats are used, so extraction
has to cope with variation the way it would with real vendors. Two render
modes simulate bad input:

* ``poor_scan``: the page is rasterised at low resolution, rotated, blurred
  and speckled, and saved as an image-only PDF (no text layer).
* ``ocr_noise``: a "scanned" page whose OCR text layer misread one digit in a
  line amount, so the arithmetic on the invoice no longer adds up.
"""

from __future__ import annotations

import io
import json
import random
from dataclasses import asdict
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from .config import BILL_TO
from .datagen import InvoiceSpec

W, H = letter

TEMPLATES = [
    {   # 0: classic
        "title": "INVOICE", "inv": "Invoice #:", "date": "Invoice Date:", "due": "Due Date:", "po": "PO #:",
        "datefmt": "%Y-%m-%d", "sub": "Subtotal", "tax": "Sales Tax", "total": "Total Due",
        "accent": colors.HexColor("#1f4e79"),
    },
    {   # 1: European-ish wording
        "title": "TAX INVOICE", "inv": "Invoice No.", "date": "Date of Issue", "due": "Payment Due",
        "po": "Purchase Order", "datefmt": "%b %d, %Y", "sub": "Net Amount", "tax": "Tax",
        "total": "Amount Due (USD)", "accent": colors.HexColor("#5b2c6f"),
    },
    {   # 2: small-business template
        "title": "Invoice", "inv": "Reference", "date": "Dated", "due": "Due", "po": "Customer PO",
        "datefmt": "%m/%d/%Y", "sub": "Subtotal", "tax": "Tax", "total": "TOTAL",
        "accent": colors.HexColor("#1e8449"),
    },
]

DIGIT_SWAP = {"0": "8", "1": "7", "2": "7", "3": "8", "4": "9", "5": "6", "6": "5", "7": "1", "8": "3", "9": "4"}


def _fmt_money(x: float) -> str:
    return f"{x:,.2f}"


def _fmt_date(iso: str, fmt: str) -> str:
    return date.fromisoformat(iso).strftime(fmt)


def _misread(amount_str: str, rng: random.Random) -> str:
    idxs = [i for i, ch in enumerate(amount_str) if ch.isdigit()]
    # avoid the cents so the error is material
    idxs = idxs[: max(1, len(idxs) - 2)]
    i = rng.choice(idxs)
    return amount_str[:i] + DIGIT_SWAP[amount_str[i]] + amount_str[i + 1:]


def _draw(c: canvas.Canvas, spec: InvoiceSpec, rng: random.Random) -> None:
    t = TEMPLATES[spec.template]
    accent = t["accent"]

    # vendor block
    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, H - 60, spec.vendor_display_name)
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 75, f"{rng.randint(100, 9999)} {rng.choice(['Market', 'Pine', 'Oak', 'Mission', 'Lake'])} "
                             f"{rng.choice(['Street', 'Avenue', 'Blvd'])}, {rng.choice(['Oakland', 'Reno', 'Portland', 'Austin', 'Denver'])}")
    c.drawString(50, H - 87, f"Remit to: accounts@{spec.vendor_display_name.split()[0].lower()}.example")

    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - 50, H - 62, t["title"])

    if spec.scenario == "duplicate":
        c.setFillColor(colors.HexColor("#b03a2e"))
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(W - 50, H - 80, "PAYMENT REMINDER - PLEASE REMIT")
        c.setFillColor(colors.black)

    # header fields
    y = H - 125
    fields = [(t["inv"], spec.invoice_number),
              (t["date"], _fmt_date(spec.invoice_date, t["datefmt"])),
              (t["due"], _fmt_date(spec.due_date, t["datefmt"]))]
    if spec.printed_po:
        fields.append((t["po"], spec.printed_po))
    for label, value in fields:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(360, y, label)
        c.setFont("Helvetica", 10)
        c.drawString(460, y, value)
        y -= 15

    # bill to
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, H - 125, "Bill To:")
    c.setFont("Helvetica", 10)
    for i, line in enumerate([BILL_TO["name"], *BILL_TO["address"]]):
        c.drawString(50, H - 140 - 13 * i, line)

    # line items
    y = H - 230
    c.setFillColor(accent)
    c.rect(45, y - 4, W - 90, 16, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y, "Line")
    c.drawString(78, y, "Item")
    c.drawString(170, y, "Description")
    c.drawRightString(400, y, "Qty")
    c.drawRightString(475, y, "Unit Price")
    c.drawRightString(W - 52, y, "Amount")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    y -= 20
    for i, li in enumerate(spec.lines):
        amount = _fmt_money(li["amount"])
        if spec.render == "ocr_noise" and i == spec.noise.get("line_index"):
            amount = _misread(amount, rng)
        qty = li["quantity"]
        c.drawString(52, y, str(li["line_no"]))
        c.drawString(78, y, li["sku"])
        c.drawString(170, y, li["description"])
        c.drawRightString(400, y, f"{qty:g}")
        c.drawRightString(475, y, _fmt_money(li["unit_price"]))
        c.drawRightString(W - 52, y, amount)
        y -= 16

    # totals
    y -= 10
    c.line(360, y + 8, W - 50, y + 8)
    for label, value, bold in [(t["sub"], spec.subtotal, False), (t["tax"], spec.tax, False),
                               (t["total"], spec.total, True)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawString(360, y - 6, label)
        c.drawRightString(W - 52, y - 6, f"${_fmt_money(value)}")
        y -= 18

    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(colors.grey)
    c.drawString(50, 60, "Thank you for your business. Please reference the invoice number with your payment.")

    if spec.render == "ocr_noise":
        # scanner speckle so it looks like a scan with an OCR text layer
        c.setFillColor(colors.Color(0, 0, 0, alpha=0.25))
        for _ in range(250):
            c.circle(rng.uniform(0, W), rng.uniform(0, H), rng.uniform(0.2, 0.9), fill=1, stroke=0)


def _to_poor_scan(pdf_bytes: bytes, rng: random.Random) -> bytes:
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    page = pdfium.PdfDocument(pdf_bytes)[0]
    img = page.render(scale=70 / 72).to_pil().convert("L")
    img = img.rotate(rng.uniform(-2.5, 2.5), expand=True, fillcolor=235)
    img = img.filter(ImageFilter.GaussianBlur(radius=1.1))
    px = img.load()
    for _ in range(int(img.size[0] * img.size[1] * 0.02)):
        x, y = rng.randrange(img.size[0]), rng.randrange(img.size[1])
        px[x, y] = rng.choice([0, 60, 200])
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PDF", resolution=70)
    return out.getvalue()


def render_invoice(spec: InvoiceSpec, seed: int = 0) -> bytes:
    rng = random.Random(f"{seed}-{spec.file_name}")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"Invoice {spec.invoice_number}")
    c.setAuthor(spec.vendor_display_name)
    _draw(c, spec, rng)
    c.showPage()
    c.save()
    data = buf.getvalue()
    if spec.render == "poor_scan":
        data = _to_poor_scan(data, rng)
    return data


def write_invoices(specs: list[InvoiceSpec], out_dir: Path, manifest_path: Path, seed: int = 0) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.pdf"):
        old.unlink()
    for spec in specs:
        (out_dir / spec.file_name).write_bytes(render_invoice(spec, seed))
    manifest = [asdict(s) for s in specs]
    manifest_path.write_text(json.dumps(manifest, indent=2))
