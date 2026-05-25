from decimal import Decimal

from django.test import SimpleTestCase

from invoices.services.project_file_invoice_service import ProjectFileInvoiceService


class ProjectFileInvoiceRatioTests(SimpleTestCase):
    def _summary(self, total_sub, remaining_sub):
        return {
            "total_invoiceable_subtotal": Decimal(str(total_sub)),
            "remaining_subtotal": Decimal(str(remaining_sub)),
        }

    def test_percentage_100_uses_remaining_not_full_total(self):
        """After partial invoicing, 100% must bill only the pending subtotal."""
        summary = self._summary(total_sub="84.79", remaining_sub="60.00")
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
            summary, "percentage", Decimal("100")
        )
        self.assertEqual(ratio, Decimal("60.00") / Decimal("84.79"))

    def test_percentage_50_of_remaining(self):
        summary = self._summary(total_sub="100", remaining_sub="40")
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
            summary, "percentage", Decimal("50")
        )
        self.assertEqual(ratio, Decimal("0.20"))

    def test_custom_full_remaining_after_partial_invoice(self):
        """Invoicing exactly the pending subtotal must not use ratio=1 on all lines."""
        summary = self._summary(total_sub="84.79", remaining_sub="60.00")
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
            summary, "custom", Decimal("100"), custom_amount=Decimal("60")
        )
        self.assertEqual(ratio, Decimal("60") / Decimal("84.79"))

    def test_custom_partial_amount(self):
        summary = self._summary(total_sub="100", remaining_sub="40")
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
            summary, "custom", Decimal("100"), custom_amount=Decimal("20")
        )
        self.assertEqual(ratio, Decimal("0.20"))
