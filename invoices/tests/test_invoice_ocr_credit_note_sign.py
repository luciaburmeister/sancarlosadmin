"""Credit notes (abonos) read as positive by the OCR are flipped to negative."""
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from core.services.gemini_extraction_service import BaseGeminiExtractor
from invoices.services.ocr_services.gemini_ocr import GeminiInvoiceProcessor
from invoices.services.ocr_services.ocr_interface import InvoiceData, LineItem, TaxItem


def _data(document_type, sign=1):
    d = Decimal
    return InvoiceData(
        document_type=document_type,
        vendor_name="V", vendor_tax_id=None, company_tax_id=None,
        invoice_number="A-1", date_issued=None, due_date=None,
        net_amount=d("100") * sign, total_amount=d("121") * sign,
        currency="EUR", payment_method=None,
        line_items=[LineItem("x", d("2"), d("50") * sign, None, d("100") * sign, d("100") * sign, d("21"))],
        taxes=[TaxItem("IVA", d("21"), d("100") * sign, d("21") * sign)],
    )


class CreditNoteSignTests(SimpleTestCase):
    def _run(self, data):
        with mock.patch.object(BaseGeminiExtractor, "extract_data", return_value=data):
            return GeminiInvoiceProcessor.__new__(GeminiInvoiceProcessor).extract_data("f.pdf")

    def test_positive_credit_note_is_flipped(self):
        out = self._run(_data("credit_note"))
        self.assertEqual(out.net_amount, Decimal("-100"))
        self.assertEqual(out.total_amount, Decimal("-121"))
        line = out.line_items[0]
        self.assertEqual((line.unit_price, line.gross_total, line.net_total),
                         (Decimal("-50"), Decimal("-100"), Decimal("-100")))
        self.assertEqual(line.quantity, Decimal("2"))
        self.assertEqual(line.tax_rate, Decimal("21"))
        self.assertEqual(out.taxes[0].tax_amount, Decimal("-21"))
        self.assertEqual(out.taxes[0].percentage, Decimal("21"))

    def test_already_negative_credit_note_unchanged(self):
        out = self._run(_data("credit_note", sign=-1))
        self.assertEqual(out.total_amount, Decimal("-121"))

    def test_regular_invoice_untouched(self):
        out = self._run(_data("invoice"))
        self.assertEqual(out.total_amount, Decimal("121"))

    def test_separate_discount_line_keeps_relative_sign(self):
        data = _data("credit_note")
        data.line_items.append(
            LineItem("Descuento", Decimal("1"), Decimal("-10"), None, Decimal("-10"), Decimal("-10"), Decimal("21"))
        )
        out = self._run(data)
        self.assertEqual(out.line_items[1].net_total, Decimal("10"))
        self.assertEqual(out.line_items[0].net_total, Decimal("-100"))
