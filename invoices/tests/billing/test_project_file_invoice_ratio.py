from decimal import Decimal

from django.test import SimpleTestCase

from invoices.services.project_file_invoice_service import ProjectFileInvoiceService


class ProjectFileInvoiceRatioTests(SimpleTestCase):
    """The line ratio is always a fraction of the *total* invoiceable subtotal
    so that, after a partial invoice, 100 % (or a custom amount equal to the
    pending subtotal) bills only what is pending instead of re-billing the
    whole expediente with ratio=1."""

    def test_ratio_is_a_fraction_of_the_total_subtotal(self):
        cases = [
            # (label, total_sub, remaining_sub, mode, percentage, custom_amount, expected)
            ("100% of the pending subtotal after a partial invoice",
             "84.79", "60.00", "percentage", "100", None,
             Decimal("60.00") / Decimal("84.79")),
            ("50% of the pending subtotal",
             "100", "40", "percentage", "50", None, Decimal("0.20")),
            ("custom amount equal to the pending subtotal",
             "84.79", "60.00", "custom", "100", "60",
             Decimal("60") / Decimal("84.79")),
            ("custom partial amount",
             "100", "40", "custom", "100", "20", Decimal("0.20")),
        ]
        for label, total_sub, remaining_sub, mode, percentage, custom, expected in cases:
            with self.subTest(label):
                summary = {
                    "total_invoiceable_subtotal": Decimal(total_sub),
                    "remaining_subtotal": Decimal(remaining_sub),
                }
                ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
                    summary, mode, Decimal(percentage),
                    custom_amount=None if custom is None else Decimal(custom),
                )
                self.assertEqual(ratio, expected)
