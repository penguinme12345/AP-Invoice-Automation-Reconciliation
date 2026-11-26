from decimal import Decimal

import pytest
from pydantic import ValidationError

from ap_automation.models import Invoice, RawInvoice, RawLineItem


def raw(**over) -> RawInvoice:
    base = dict(
        vendor_name="Cobalt Office Supply LLC", invoice_number="INV-001234", po_number="PO-10001",
        invoice_date="2026-10-01", due_date="2026-10-31", currency="USD",
        line_items=[RawLineItem(line_no=1, sku="COS-PAP-500", description="Copy paper", quantity=10,
                                unit_price=45.10, amount=451.00),
                    RawLineItem(line_no=2, sku="COS-TON-26A", description="Toner", quantity=2,
                                unit_price=100.00, amount=200.00)],
        subtotal=651.00, tax=56.15, total=707.15,
    )
    base.update(over)
    return RawInvoice(**base)


def test_valid_invoice_passes_gate():
    inv = Invoice.from_raw(raw())
    assert inv.total == Decimal("707.15")
    assert inv.po_number == "PO-10001"
    assert len(inv.line_items) == 2


def test_missing_po_is_allowed_and_left_for_the_match():
    assert Invoice.from_raw(raw(po_number=None)).po_number is None
    assert Invoice.from_raw(raw(po_number="N/A")).po_number is None


@pytest.mark.parametrize("printed", ["po 10001", "PO10001", " PO-10001 "])
def test_po_number_is_normalised(printed):
    assert Invoice.from_raw(raw(po_number=printed)).po_number == "PO-10001"


def test_garbage_po_is_rejected():
    with pytest.raises(ValidationError, match="PO-NNNNN"):
        Invoice.from_raw(raw(po_number="ABC"))


def test_totals_that_do_not_add_up_are_rejected():
    with pytest.raises(ValidationError, match="subtotal"):
        Invoice.from_raw(raw(total=799.99))


def test_line_math_is_checked():
    bad = raw(line_items=[RawLineItem(line_no=1, description="Copy paper", quantity=10, unit_price=45.10,
                                      amount=491.00)], subtotal=491.00, tax=0, total=491.00)
    with pytest.raises(ValidationError, match="quantity x unit price"):
        Invoice.from_raw(bad)


@pytest.mark.parametrize("field", ["vendor_name", "invoice_number", "invoice_date", "total"])
def test_required_fields(field):
    with pytest.raises(ValidationError):
        Invoice.from_raw(raw(**{field: None}))


def test_no_line_items_rejected():
    with pytest.raises(ValidationError):
        Invoice.from_raw(raw(line_items=[]))


def test_due_date_before_invoice_date_rejected():
    with pytest.raises(ValidationError, match="due date"):
        Invoice.from_raw(raw(due_date="2026-09-01"))
