"""Invoice extraction with LangChain, validated with Pydantic.

Two interchangeable LangChain chains produce a ``RawInvoice``:

* ``llm``     - Claude through ``langchain-anthropic``. The PDF itself is sent
                as a document block, so the model reads text layers and scanned
                images alike, and ``with_structured_output`` returns a
                ``RawInvoice`` via the structured-outputs JSON schema.
* ``offline`` - a deterministic rule-based Runnable over the PDF text layer.
                Used for the no-credentials demo and in CI so results are
                reproducible. It cannot read image-only scans, which is
                exactly the case that should go to a human or the LLM.

Whatever produced it, the ``RawInvoice`` then goes through ``Invoice.from_raw``,
the Pydantic quality gate. A failure there is recorded as
``EXTRACTION_FAILED`` instead of being saved as if it were good data.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pdfplumber
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import ValidationError

from .models import Invoice, RawInvoice, RawLineItem

SYSTEM_PROMPT = """You are an accounts-payable data entry specialist. Extract the fields of the vendor \
invoice in the attached PDF exactly as printed.

Rules:
- vendor_name is the company that issued the invoice (the seller), not the Bill To customer.
- Copy numbers exactly as printed, even if they look wrong. Do not fix arithmetic, do not \
recalculate totals: a separate validation step checks the math.
- Use null for any field that is not printed or that you cannot read with confidence. Never guess \
a PO number.
- Dates in ISO format YYYY-MM-DD. Money as plain numbers without currency symbols or thousands separators.
- One line_items entry per billed line, in printed order."""


@dataclass
class Document:
    path: Path
    data: bytes
    text: str

    @property
    def has_text_layer(self) -> bool:
        return len(self.text.strip()) > 20


@dataclass
class ExtractionResult:
    source_file: str
    extractor: str
    raw: Optional[RawInvoice]
    invoice: Optional[Invoice]
    error: Optional[str]

    @property
    def ok(self) -> bool:
        return self.invoice is not None


def load_document(path: Path | str) -> Document:
    path = Path(path)
    data = path.read_bytes()
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return Document(path=path, data=data, text=text)


# --------------------------------------------------------------------------- #
# Offline rule-based extractor
# --------------------------------------------------------------------------- #
_DATE = r"(\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2} \d{1,2}, \d{4}|\d{2}/\d{2}/\d{4})"
_MONEY = r"\$?\s?([\d,]+\.\d{2})"
_TITLES = ("TAX INVOICE", "INVOICE", "Invoice")
_LINE_RE = re.compile(
    r"^(\d{1,3})\s+([A-Z0-9][A-Z0-9-]+)\s+(.+?)\s+(\d+(?:\.\d+)?)\s+([\d,]+\.\d{2})\s+(\S+)$"
)


def _parse_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _num(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _first(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def rule_extract(doc: Document) -> RawInvoice:
    text = doc.text
    if not doc.has_text_layer:
        return RawInvoice()
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    vendor = lines[0]
    for t in _TITLES:
        if vendor.endswith(" " + t):
            vendor = vendor[: -len(t) - 1].strip()
            break

    items: list[RawLineItem] = []
    for l in lines:
        m = _LINE_RE.match(l)
        if m:
            items.append(RawLineItem(
                line_no=int(m.group(1)), sku=m.group(2), description=m.group(3),
                quantity=_num(m.group(4)), unit_price=_num(m.group(5)),
                # an OCR misread can leave non-numeric junk here -> None -> caught by validation
                amount=_num(m.group(6)),
            ))

    return RawInvoice(
        vendor_name=vendor,
        invoice_number=_first(r"(?:Invoice #:|Invoice No\.|Reference)\s+(\S+)", text),
        po_number=_first(r"(?:PO #:|Purchase Order|Customer PO)\s+(\S+)", text),
        invoice_date=_parse_date(_first(r"(?:Invoice Date:|Date of Issue|Dated)\s+" + _DATE, text)),
        due_date=_parse_date(_first(r"(?:Due Date:|Payment Due|Due)\s+" + _DATE, text)),
        currency="USD" if "$" in text or "USD" in text else None,
        line_items=items,
        subtotal=_num(_first(r"(?:Subtotal|Net Amount)\s+" + _MONEY, text)),
        tax=_num(_first(r"(?:Sales Tax|Tax)\s+" + _MONEY, text)),
        total=_num(_first(r"(?:Total Due|Amount Due \(USD\)|TOTAL)\s+" + _MONEY, text)),
    )


def build_offline_chain() -> Runnable:
    return RunnableLambda(load_document) | RunnableLambda(rule_extract)


# --------------------------------------------------------------------------- #
# LLM extractor (Claude via LangChain)
# --------------------------------------------------------------------------- #
def _to_messages(doc: Document):
    from langchain_core.messages import HumanMessage, SystemMessage

    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=[
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.b64encode(doc.data).decode()},
            },
            {"type": "text", "text": "Extract this invoice."},
        ]),
    ]


def build_llm_chain(model: str = "claude-opus-5-5", llm=None) -> Runnable:
    """LangChain pipeline: load PDF -> prompt -> Claude structured output -> RawInvoice.

    ``llm`` can be injected (tests pass a stub) - otherwise ChatAnthropic is
    created and reads ANTHROPIC_API_KEY from the environment.
    """
    if llm is None:
        from langchain_anthropic import ChatAnthropic

        llm = ChatAnthropic(model=model, max_tokens=8000, max_retries=3)
    # method="json_schema" uses Claude structured outputs (forced tool choice is not
    # supported on the newest models).
    structured = llm.with_structured_output(RawInvoice, method="json_schema")
    return RunnableLambda(load_document) | RunnableLambda(_to_messages) | structured


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def format_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "invoice"
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}")
    return "; ".join(parts)


class InvoiceExtractor:
    def __init__(self, mode: str = "offline", model: str = "claude-opus-5-5", chain: Runnable | None = None):
        self.mode = mode
        if chain is not None:
            self.chain = chain
        elif mode == "llm":
            self.chain = build_llm_chain(model)
        elif mode == "offline":
            self.chain = build_offline_chain()
        else:
            raise ValueError(f"unknown extractor mode {mode!r}")

    def extract(self, path: Path | str) -> ExtractionResult:
        path = Path(path)
        try:
            raw = self.chain.invoke(path)
        except Exception as e:  # network / model / parse errors: never crash the batch
            return ExtractionResult(path.name, self.mode, None, None, f"extractor error: {type(e).__name__}: {e}")
        if isinstance(raw, dict):
            raw = RawInvoice.model_validate(raw)
        if raw == RawInvoice():
            return ExtractionResult(path.name, self.mode, raw, None,
                                    "no readable text (image-only or unreadable scan); needs OCR/vision or manual entry")
        try:
            return ExtractionResult(path.name, self.mode, raw, Invoice.from_raw(raw), None)
        except ValidationError as e:
            return ExtractionResult(path.name, self.mode, raw, None, format_validation_error(e))
