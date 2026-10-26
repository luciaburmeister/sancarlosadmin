"""The report_irpf_sign_affected command lists documents stored with the IRPF
retention added, and writes nothing."""
from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from company.models import Company
from invoices.models import AppliedTax, SalesInvoice, SalesInvoiceStatus, TaxType
from sales.models import Client, Quote


class ReportIrpfSignAffectedTests(TestCase):
    """1000 of base, IVA 21 % (210) and IRPF 15 % (150): stored with the
    retention added the tax is 360 and the total 1360; netted, 60 and 1060."""

    def setUp(self):
        self.company = Company.objects.create(name="Report Co")
        self.client_obj = Client.objects.create(name="Autónomo", company=self.company)
        self.iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )
        self.irpf, _ = TaxType.objects.get_or_create(
            tax_name="IRPF", country="ES", defaults={"behavior": "subtract"}
        )
        self.added_quote = self._quote(tax=Decimal("360.00"), total=Decimal("1360.00"))
        self.netted_quote = self._quote(tax=Decimal("60.00"), total=Decimal("1060.00"))
        self.added_invoice = self._invoice(
            "INV-IRPF-ADDED", tax=Decimal("360.00"), total=Decimal("1360.00"),
        )
        self.netted_invoice = self._invoice(
            "INV-IRPF-NETTED", tax=Decimal("60.00"), total=Decimal("1060.00"),
        )

    def _add_taxes(self, obj):
        ct = ContentType.objects.get_for_model(obj)
        for order, (tax_type, pct, calc) in enumerate((
            (self.iva, Decimal("21"), Decimal("210.00")),
            (self.irpf, Decimal("15"), Decimal("150.00")),
        )):
            AppliedTax.objects.create(
                content_type=ct, object_id=obj.pk, tax_type=tax_type,
                selected_percentage=pct, base_amount=Decimal("1000.00"),
                calculated_amount=calc, order=order,
            )

    def _quote(self, tax, total):
        quote = Quote.objects.create(
            project_name="IRPF quote", company=self.company, client=self.client_obj,
        )
        self._add_taxes(quote)
        Quote.objects.filter(pk=quote.pk).update(
            subtotal_price=Decimal("1000.00"), discounted_subtotal=Decimal("1000.00"),
            tax_amount=tax, total_price=total,
        )
        return quote

    def _invoice(self, number, tax, total):
        invoice = SalesInvoice.objects.create(
            company=self.company, client=self.client_obj,
            status=SalesInvoiceStatus.PROFORMA, date_issued=date(2026, 9, 1),
        )
        self._add_taxes(invoice)
        # Issued figures, as a builder stored them (no recalculation).
        SalesInvoice.objects.filter(pk=invoice.pk).update(
            status=SalesInvoiceStatus.FINAL, invoice_number=number,
            subtotal_amount=Decimal("1000.00"), tax_amount=tax, total_amount=total,
        )
        return invoice

    def _run(self, **options):
        out = StringIO()
        call_command("report_irpf_sign_affected", stdout=out, **options)
        return out.getvalue()

    def test_lists_only_documents_with_the_retention_added(self):
        output = self._run()
        self.assertIn(f"Quote #{self.added_quote.pk} ", output)
        self.assertNotIn(f"Quote #{self.netted_quote.pk} ", output)
        self.assertIn("INV-IRPF-ADDED", output)
        self.assertNotIn("INV-IRPF-NETTED", output)
        # stored 1360.00, netted 1360 - 2 * 150 = 1060.00
        self.assertIn("1360.00", output)
        self.assertIn("1060.00", output)

    def test_company_filter(self):
        other = Company.objects.create(name="Other Co")
        output = self._run(company=other.pk)
        self.assertNotIn(f"Quote #{self.added_quote.pk} ", output)
        self.assertNotIn("INV-IRPF-ADDED", output)

    def test_writes_nothing(self):
        with CaptureQueriesContext(connection) as ctx:
            self._run()
        writes = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])
        self.added_invoice.refresh_from_db()
        self.added_quote.refresh_from_db()
        self.assertEqual(self.added_invoice.total_amount.amount, Decimal("1360.00"))
        self.assertEqual(self.added_quote.total_price.amount, Decimal("1360.00"))
