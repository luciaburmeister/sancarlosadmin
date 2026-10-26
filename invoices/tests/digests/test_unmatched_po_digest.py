from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from djmoney.money import Money

from inventory.models import InventoryItem
from invoices.models import (
    Invoice,
    InvoiceDocumentType,
    InvoiceItem,
    InvoicePOAllocation,
)
from invoices.services.unmatched_po_digest_email_service import (
    UnmatchedPODigestEmailService,
)
from invoices.services.unmatched_po_digest_service import UnmatchedPODigestService
from invoices.tasks import send_unmatched_po_digest
from invoices.tests.digest_fixtures import make_company, make_purchase_invoice
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
)


@patch("core.services.email_service.EmailService.send_email", return_value=True)
class SendUnmatchedPODigestTests(TestCase):
    """POL-249: daily report of purchase invoices still missing their PO link."""

    STALE_HOURS = UnmatchedPODigestService.GRACE_HOURS + 1

    def _make_company(self, name="Alpha", emails=None):
        return make_company(
            name=name,
            unmatched_po_digest_emails=["compras@example.com"]
            if emails is None
            else emails,
        )

    def _make_invoice(
        self,
        company,
        number="F-001",
        total="100.00",
        age_hours=None,
        quantity="10",
        **kwargs,
    ):
        """Create an invoice with one line, aged `age_hours` into the past.

        created_at is auto_now_add, so it needs a follow-up update() to
        backdate it past the grace period.
        """
        age_hours = self.STALE_HOURS if age_hours is None else age_hours
        invoice = make_purchase_invoice(
            company=company, number=number, total=total, **kwargs,
        )
        if quantity is not None:
            InvoiceItem.objects.create(
                invoice=invoice,
                description=f"Línea {number}",
                quantity=Decimal(quantity),
                unit_price=Money("10.00", "EUR"),
                line_total=Money(total, "EUR"),
            )
        Invoice.objects.filter(pk=invoice.pk).update(
            created_at=timezone.now() - timedelta(hours=age_hours)
        )
        invoice.refresh_from_db()
        return invoice

    def _allocate(self, invoice, quantity):
        """Match `quantity` of the invoice's first line against a PO line."""
        po = PurchaseOrder.objects.create(
            company=invoice.company,
            provider=invoice.provider,
            number=f"PO-{PurchaseOrder.objects.count() + 1}",
            status=PurchaseOrderStatus.APPROVED,  # only these can be invoiced
        )
        item = InventoryItem.objects.create(company=invoice.company, name="Perfil")
        po_item = PurchaseOrderItem.objects.create(
            purchase_order=po,
            inventory_item=item,
            quantity=Decimal("100"),
            unit_price=Money("10.00", "EUR"),
        )
        invoice_item = invoice.items.first()
        return InvoicePOAllocation.objects.create(
            invoice=invoice,
            invoice_item=invoice_item,
            po_item=po_item,
            quantity=Decimal(quantity),
        )

    def _digest_for(self, company):
        digests = UnmatchedPODigestService.get_company_digests([company])
        return digests[company.pk]

    # ── Which invoices qualify ────────────────────────────────────────

    def test_reports_invoice_with_no_allocation(self, _send):
        company = self._make_company()
        invoice = self._make_invoice(company)

        digest = self._digest_for(company)

        self.assertEqual(digest.count, 1)
        row = digest.rows[0]
        self.assertEqual(row.invoice, invoice)
        self.assertFalse(row.is_partial)
        self.assertEqual(row.uncovered_amount, Decimal("100.00"))

    def test_reports_partially_matched_invoice(self, _send):
        company = self._make_company()
        invoice = self._make_invoice(company, quantity="10", total="100.00")
        self._allocate(invoice, "4")

        digest = self._digest_for(company)

        self.assertEqual(digest.count, 1)
        row = digest.rows[0]
        self.assertTrue(row.is_partial)
        # 6 of 10 units uncovered -> 60% of 100.00
        self.assertEqual(row.uncovered_amount, Decimal("60.00"))
        self.assertEqual(digest.partial_count, 1)

    def test_skips_fully_matched_invoice(self, _send):
        company = self._make_company()
        invoice = self._make_invoice(company, quantity="10")
        self._allocate(invoice, "10")

        self.assertEqual(self._digest_for(company).count, 0)

    def test_skips_invoice_flagged_as_no_relevant_po(self, _send):
        """skip_po_matching is the authorisation exemption (fuel, landfill…)."""
        company = self._make_company()
        self._make_invoice(company, skip_po_matching=True)

        self.assertEqual(self._digest_for(company).count, 0)

    def test_skips_invoice_inside_grace_period(self, _send):
        company = self._make_company()
        self._make_invoice(company, age_hours=UnmatchedPODigestService.GRACE_HOURS - 1)

        self.assertEqual(self._digest_for(company).count, 0)

    def test_skips_credit_notes(self, _send):
        company = self._make_company()
        self._make_invoice(
            company, document_type=InvoiceDocumentType.CREDIT_NOTE
        )

        self.assertEqual(self._digest_for(company).count, 0)

    def test_reports_invoice_with_no_lines_at_full_amount(self, _send):
        """OCR failed to extract lines: nothing about it is authorised."""
        company = self._make_company()
        self._make_invoice(company, quantity=None, total="250.00")

        digest = self._digest_for(company)

        self.assertEqual(digest.count, 1)
        self.assertEqual(digest.rows[0].uncovered_amount, Decimal("250.00"))

    def test_reports_invoice_registered_on_start_date(self, _send):
        company = self._make_company()
        invoice = self._make_invoice(company, age_hours=24 * 3)
        company.unmatched_po_digest_start_date = timezone.localdate(
            invoice.created_at
        )
        company.save()

        self.assertEqual(self._digest_for(company).count, 1)

    def test_start_date_is_per_company(self, _send):
        """Old invoices nobody will match retroactively stay out of the digest;
        a company with no start date gets every invoice reported."""
        alpha = self._make_company("Alpha")
        beta = self._make_company("Beta")
        alpha.unmatched_po_digest_start_date = timezone.localdate()
        alpha.save()
        beta.unmatched_po_digest_start_date = None
        beta.save()
        self._make_invoice(alpha, number="F-A", age_hours=24 * 5)
        self._make_invoice(beta, number="F-B", age_hours=24 * 5)

        digests = UnmatchedPODigestService.get_company_digests([alpha, beta])

        self.assertEqual(digests[alpha.pk].count, 0)
        self.assertEqual(digests[beta.pk].count, 1)

    def test_start_date_can_be_overridden(self, _send):
        company = self._make_company()
        company.unmatched_po_digest_start_date = timezone.localdate()
        company.save()
        self._make_invoice(company, age_hours=24 * 5)

        digests = UnmatchedPODigestService.get_company_digests(
            [company], start_date=timezone.localdate() - timedelta(days=30)
        )

        self.assertEqual(digests[company.pk].count, 1)

    def test_grace_hours_is_per_company(self, _send):
        """The threshold is editable per company from the admin."""
        alpha = self._make_company("Alpha")
        beta = self._make_company("Beta")
        alpha.unmatched_po_digest_grace_hours = 4
        alpha.save()
        beta.unmatched_po_digest_grace_hours = 72
        beta.save()
        self._make_invoice(alpha, number="F-A", age_hours=6)
        self._make_invoice(beta, number="F-B", age_hours=6)

        digests = UnmatchedPODigestService.get_company_digests([alpha, beta])

        self.assertEqual(digests[alpha.pk].grace_hours, 4)
        self.assertEqual(digests[alpha.pk].count, 1)  # past its 4h threshold
        self.assertEqual(digests[beta.pk].grace_hours, 72)
        self.assertEqual(digests[beta.pk].count, 0)  # still inside its 72h one

    def test_only_reports_invoices_of_requested_companies(self, _send):
        company = self._make_company("Alpha")
        other = self._make_company("Beta")
        self._make_invoice(other, number="F-BETA")

        self.assertEqual(self._digest_for(company).count, 0)
        self.assertEqual(self._digest_for(other).count, 1)

    def test_runs_in_a_single_query_regardless_of_company_count(self, _send):
        alpha = self._make_company("Alpha")
        beta = self._make_company("Beta")
        for company in (alpha, beta):
            self._make_invoice(company, number=f"F-{company.name}")

        with self.assertNumQueries(1):
            digests = UnmatchedPODigestService.get_company_digests([alpha, beta])
            [row for digest in digests.values() for row in digest.rows]

    def test_allocations_on_one_line_are_not_double_counted(self, _send):
        """A joined Sum() would fan out and hide a partially matched line."""
        company = self._make_company()
        invoice = self._make_invoice(company, quantity="10", total="100.00")
        self._allocate(invoice, "3")
        self._allocate(invoice, "2")

        digest = self._digest_for(company)

        self.assertEqual(digest.count, 1)
        self.assertEqual(digest.rows[0].matched_quantity, Decimal("5"))
        self.assertEqual(digest.rows[0].uncovered_amount, Decimal("50.00"))

    # ── Totals ────────────────────────────────────────────────────────

    def test_totals_sum_uncovered_amounts(self, _send):
        company = self._make_company()
        self._make_invoice(company, number="F-1", total="100.00")
        self._make_invoice(company, number="F-2", total="50.00")

        digest = self._digest_for(company)

        self.assertEqual(digest.count, 2)
        self.assertEqual(digest.totals["EUR"], Decimal("150.00"))

    # ── Email construction ────────────────────────────────────────────

    def test_email_lists_invoice_and_links_to_matching_screen(self, _send):
        company = self._make_company()
        invoice = self._make_invoice(company, number="F-777")

        emails = UnmatchedPODigestEmailService.build_emails(
            [company],
            UnmatchedPODigestService.get_company_digests([company]),
            today=timezone.localdate(),
        )

        self.assertEqual(len(emails), 1)
        email = emails[0]
        self.assertEqual(email.recipients, ["compras@example.com"])
        self.assertIn("F-777", email.body_html)
        self.assertIn(f"/invoices/invoice/{invoice.pk}/match-po/", email.body_html)
        self.assertIn("(1)", email.subject)

    def test_no_email_when_nothing_to_report(self, _send):
        company = self._make_company()
        self._make_invoice(company, skip_po_matching=True)

        emails = UnmatchedPODigestEmailService.build_emails(
            [company],
            UnmatchedPODigestService.get_company_digests([company]),
            today=timezone.localdate(),
        )

        self.assertEqual(emails, [])

    def test_recipient_on_two_companies_gets_one_consolidated_email(self, _send):
        alpha = self._make_company("Alpha", ["shared@example.com"])
        beta = self._make_company("Beta", ["shared@example.com"])
        self._make_invoice(alpha, number="F-A")
        self._make_invoice(beta, number="F-B")

        companies = [alpha, beta]
        emails = UnmatchedPODigestEmailService.build_emails(
            companies,
            UnmatchedPODigestService.get_company_digests(companies),
            today=timezone.localdate(),
        )

        self.assertEqual(len(emails), 1)
        self.assertIn("F-A", emails[0].body_html)
        self.assertIn("F-B", emails[0].body_html)
        self.assertIsNone(emails[0].company)  # consolidated: no single attribution

    # ── Task orchestration ────────────────────────────────────────────

    def test_task_sends_email_and_reports_count(self, send_email):
        company = self._make_company()
        self._make_invoice(company)

        result = send_unmatched_po_digest()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["emails_sent"], 1)
        self.assertEqual(send_email.call_count, 1)

    def test_task_skips_companies_without_recipients(self, send_email):
        company = self._make_company(emails=[])
        self._make_invoice(company)

        result = send_unmatched_po_digest()

        self.assertEqual(result["emails_sent"], 0)
        send_email.assert_not_called()

    def test_task_accepts_grace_hours_override(self, send_email):
        company = self._make_company()
        self._make_invoice(company, age_hours=10)

        self.assertEqual(send_unmatched_po_digest()["emails_sent"], 0)
        self.assertEqual(send_unmatched_po_digest(grace_hours=5)["emails_sent"], 1)
