"""Pydantic models for invoice extraction.

There are two layers on purpose:

* ``RawInvoice`` is the shape the extractor (LLM or rule-based) is asked to
  fill. It is permissive: every field is optional, so the model can say
  "I could not find this" instead of inventing a value.
* ``Invoice`` is the strict, validated record that is allowed into the
  database. Converting ``RawInvoice`` -> ``Invoice`` is the quality gate:
  missing required fields, bad formats or totals that do not add up raise a
  ``ValidationError`` and the invoice is routed to an exception instead of
  being silently saved.

A missing PO number is *not* a validation error. It is a legitimate business
exception that the three-way match reports as ``MISSING_PO``.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PO_PATTERN = re.compile(r"^PO-\d{5}$")
CENT = Decimal("0.01")
# Rounding slack when checking arithmetic printed on the invoice.
ARITH_SLACK = Decimal("0.02")


def _money(v: Decimal) -> Decimal:
    return Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Extraction target (what the LLM fills in)
# --------------------------------------------------------------------------- #
class RawLineItem(BaseModel):
    line_no: Optional[int] = Field(None, description="Line number as printed on the invoice")
    sku: Optional[str] = Field(None, description="Vendor item code / SKU")
    description: Optional[str] = Field(None, description="Item description")
    quantity: Optional[float] = Field(None, description="Quantity billed")
    unit_price: Optional[float] = Field(None, description="Unit price, no currency symbol")
    amount: Optional[float] = Field(None, description="Line total, no currency symbol")


class RawInvoice(BaseModel):
    """Fields to pull from a vendor invoice. Use null for anything not printed."""

    vendor_name: Optional[str] = Field(None, description="Name of the company that issued the invoice (the seller)")
    invoice_number: Optional[str] = Field(None, description="The vendor's invoice number")
    po_number: Optional[str] = Field(None, description="Buyer purchase order number, e.g. PO-10023. Null if absent")
    invoice_date: Optional[str] = Field(None, description="Invoice date, ISO format YYYY-MM-DD")
    due_date: Optional[str] = Field(None, description="Payment due date, ISO format YYYY-MM-DD")
    currency: Optional[str] = Field(None, description="ISO currency code, e.g. USD")
    line_items: list[RawLineItem] = Field(default_factory=list)
    subtotal: Optional[float] = Field(None, description="Sum of line amounts before tax")
    tax: Optional[float] = Field(None, description="Tax amount (0 if none)")
    total: Optional[float] = Field(None, description="Total amount due")


# --------------------------------------------------------------------------- #
# Validated record (what is allowed into the database)
# --------------------------------------------------------------------------- #
class LineItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    line_no: int = Field(ge=1)
    sku: Optional[str] = None
    description: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    amount: Decimal

    @field_validator("unit_price", "amount")
    @classmethod
    def _round(cls, v: Decimal) -> Decimal:
        return _money(v)

    @model_validator(mode="after")
    def _line_math(self) -> "LineItem":
        expected = _money(self.quantity * self.unit_price)
        if abs(expected - self.amount) > ARITH_SLACK:
            raise ValueError(
                f"line {self.line_no}: quantity x unit price = {expected} but amount printed is {self.amount}"
            )
        return self


class Invoice(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vendor_name: str = Field(min_length=2)
    invoice_number: str = Field(min_length=1, max_length=40)
    po_number: Optional[str] = None
    invoice_date: date
    due_date: Optional[date] = None
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    line_items: list[LineItem] = Field(min_length=1)
    subtotal: Decimal
    tax: Decimal = Decimal("0")
    total: Decimal = Field(gt=0)

    @field_validator("po_number", mode="before")
    @classmethod
    def _normalise_po(cls, v):
        if v is None:
            return None
        s = str(v).strip().upper().replace(" ", "")
        if s in {"", "N/A", "NA", "NONE", "-"}:
            return None
        m = re.search(r"(\d{5})", s)
        if s.startswith("PO") and m:
            s = f"PO-{m.group(1)}"
        if not PO_PATTERN.match(s):
            raise ValueError(f"PO number {v!r} is not in the PO-NNNNN format")
        return s

    @field_validator("invoice_number", mode="before")
    @classmethod
    def _normalise_inv(cls, v):
        return None if v is None else str(v).strip()

    @field_validator("subtotal", "tax", "total")
    @classmethod
    def _round(cls, v: Decimal) -> Decimal:
        return _money(v)

    @model_validator(mode="after")
    def _totals_reconcile(self) -> "Invoice":
        line_sum = _money(sum((li.amount for li in self.line_items), Decimal("0")))
        if abs(line_sum - self.subtotal) > ARITH_SLACK:
            raise ValueError(f"line items sum to {line_sum} but subtotal printed is {self.subtotal}")
        if abs(self.subtotal + self.tax - self.total) > ARITH_SLACK:
            raise ValueError(f"subtotal {self.subtotal} + tax {self.tax} != total {self.total}")
        if self.due_date and self.due_date < self.invoice_date:
            raise ValueError("due date is before invoice date")
        return self

    @classmethod
    def from_raw(cls, raw: RawInvoice) -> "Invoice":
        """Quality gate: raises pydantic.ValidationError on a bad extraction."""
        data = raw.model_dump()
        if data.get("currency") is None:
            data["currency"] = "USD"
        if data.get("tax") is None:
            data["tax"] = 0
        data["line_items"] = [
            {k: v for k, v in li.items() if v is not None} for li in data.get("line_items") or []
        ]
        data = {k: v for k, v in data.items() if v is not None or k == "po_number"}
        return cls.model_validate(data)
