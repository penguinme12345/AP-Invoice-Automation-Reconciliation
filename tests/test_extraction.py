from decimal import Decimal

import pytest
from langchain_core.runnables import RunnableLambda

from ap_automation.extraction import InvoiceExtractor, build_llm_chain
from ap_automation.models import RawInvoice


@pytest.fixture(scope="module")
def extractor():
    return InvoiceExtractor("offline")


def _specs(world, render="text", scenario=None):
    return [s for s in world.invoices if s.render == render and (scenario is None or s.scenario == scenario)]


@pytest.mark.parametrize("template", [0, 1, 2])
def test_offline_extraction_reads_every_layout(world_pdfs, extractor, template):
    world, inbox = world_pdfs
    specs = [s for s in _specs(world) if s.template == template][:5]
    assert specs
    for spec in specs:
        res = extractor.extract(inbox / spec.file_name)
        assert res.ok, res.error
        inv = res.invoice
        assert inv.invoice_number == spec.invoice_number
        assert inv.po_number == spec.printed_po
        assert inv.invoice_date.isoformat() == spec.invoice_date
        assert inv.due_date.isoformat() == spec.due_date
        assert inv.total == Decimal(str(spec.total))
        assert [li.sku for li in inv.line_items] == [l["sku"] for l in spec.lines]
        assert spec.vendor_display_name.startswith(inv.vendor_name.split()[0])


def test_blurry_scan_is_caught_not_saved(world_pdfs, extractor):
    world, inbox = world_pdfs
    spec = _specs(world, render="poor_scan")[0]
    res = extractor.extract(inbox / spec.file_name)
    assert not res.ok
    assert "no readable text" in res.error


def test_ocr_misread_fails_validation(world_pdfs, extractor):
    world, inbox = world_pdfs
    spec = _specs(world, render="ocr_noise")[0]
    res = extractor.extract(inbox / spec.file_name)
    assert not res.ok
    assert "quantity x unit price" in res.error


def test_missing_po_extracts_as_none(world_pdfs, extractor):
    world, inbox = world_pdfs
    spec = _specs(world, scenario="missing_po")[0]
    res = extractor.extract(inbox / spec.file_name)
    assert res.ok and res.invoice.po_number is None


class StubLLM:
    """Stands in for ChatAnthropic: records what the chain sends, returns a canned extraction."""

    def __init__(self, answer: RawInvoice):
        self.answer = answer
        self.seen = None
        self.kwargs = None

    def with_structured_output(self, schema, **kwargs):
        self.kwargs = {"schema": schema, **kwargs}

        def _run(messages):
            self.seen = messages
            return self.answer
        return RunnableLambda(_run)


def test_llm_chain_sends_pdf_and_validates_output(world_pdfs):
    world, inbox = world_pdfs
    spec = _specs(world, scenario="clean")[0]
    answer = RawInvoice(
        vendor_name=spec.vendor_display_name, invoice_number=spec.invoice_number, po_number=spec.printed_po,
        invoice_date=spec.invoice_date, due_date=spec.due_date, currency="USD",
        line_items=[{k: l[k] for k in ("line_no", "sku", "description", "quantity", "unit_price", "amount")}
                    for l in spec.lines],
        subtotal=spec.subtotal, tax=spec.tax, total=spec.total)
    stub = StubLLM(answer)
    ex = InvoiceExtractor("llm", chain=build_llm_chain(llm=stub))
    res = ex.extract(inbox / spec.file_name)
    assert res.ok, res.error
    assert stub.kwargs["schema"] is RawInvoice and stub.kwargs["method"] == "json_schema"
    doc_block = stub.seen[1].content[0]
    assert doc_block["type"] == "document" and doc_block["source"]["media_type"] == "application/pdf"


def test_llm_hallucinated_totals_are_caught(world_pdfs):
    world, inbox = world_pdfs
    spec = _specs(world, scenario="clean")[0]
    answer = RawInvoice(vendor_name="X Co", invoice_number="1", invoice_date="2026-10-01",
                        line_items=[{"line_no": 1, "description": "a", "quantity": 1, "unit_price": 10, "amount": 10}],
                        subtotal=10, tax=0, total=12)
    ex = InvoiceExtractor("llm", chain=build_llm_chain(llm=StubLLM(answer)))
    res = ex.extract(inbox / spec.file_name)
    assert not res.ok and "total" in res.error


def test_extractor_errors_do_not_crash_the_batch(world_pdfs):
    world, inbox = world_pdfs

    def boom(_):
        raise TimeoutError("model timed out")
    res = InvoiceExtractor("llm", chain=RunnableLambda(boom)).extract(inbox / world.invoices[0].file_name)
    assert not res.ok and "TimeoutError" in res.error
