"""Prices read with the tax in are brought back to net before the lines are created."""
from decimal import Decimal

from django.test import SimpleTestCase

from invoices.services.ocr_services.gemini_ocr import GeminiInvoiceProcessor
from invoices.services.ocr_services.ocr_interface import InvoiceData, LineItem, TaxItem

d = Decimal


def _data(net_amount, total_amount, *lines):
    return InvoiceData(
        document_type="invoice",
        vendor_name="V", vendor_tax_id=None, company_tax_id=None,
        invoice_number="OB-10551", date_issued=None, due_date=None,
        net_amount=net_amount, total_amount=total_amount,
        currency="EUR", payment_method=None,
        line_items=list(lines),
        taxes=[TaxItem("IVA", d("21"), net_amount, total_amount - net_amount)],
    )


def _line(quantity, unit_price, gross_total, net_total):
    return LineItem("x", d(quantity), d(unit_price), None, d(gross_total), d(net_total), d("21"))


class TaxInclusivePriceTests(SimpleTestCase):
    def _adjust(self, data):
        return GeminiInvoiceProcessor.__new__(GeminiInvoiceProcessor)._adjust_tax_inclusive_prices(data)

    def test_line_with_net_base_but_tax_in_unit_price(self):
        # Repsol receipt: price per litre with IVA, base imponible per line without
        out = self._adjust(_data(
            d("56.34"), d("68.18"),
            _line("16.05", "1.87", "30.0135", "24.79"),
            _line("33.79", "1.13", "38.1827", "31.55"),
        ))

        self.assertEqual(
            [(line.unit_price, line.gross_total, line.net_total) for line in out.line_items],
            [(d("1.5445"), d("24.80"), d("24.79")), (d("0.9337"), d("31.56"), d("31.55"))],
        )
        # The line InvoiceItem.save computes is the printed base
        self.assertEqual(
            [(line.quantity * line.unit_price).quantize(d("0.01")) for line in out.line_items],
            [d("24.79"), d("31.55")],
        )

    def test_discounted_line_keeps_its_discount_out_of_the_price(self):
        line = LineItem("x", d("10"), d("12.10"), d("50"), d("121.00"), d("50.00"), d("21"))
        out = self._adjust(_data(d("50.00"), d("60.50"), line))

        self.assertEqual(out.line_items[0].unit_price, d("10.0000"))

    def test_whole_document_with_tax_in_prices(self):
        out = self._adjust(_data(d("100.00"), d("121.00"), _line("2", "60.50", "121.00", "121.00")))

        [line] = out.line_items
        self.assertEqual((line.unit_price, line.net_total), (d("50.00"), d("100.00")))

    def test_net_lines_are_left_alone(self):
        out = self._adjust(_data(d("100.00"), d("121.00"), _line("2", "50.00", "100.00", "100.00")))

        [line] = out.line_items
        self.assertEqual((line.unit_price, line.net_total), (d("50.00"), d("100.00")))
