"""Due date and payment commitment date are visible on the sales invoice
changelist and included in the export."""
from datetime import date, datetime

from django.conf import settings
from django.contrib.admin.sites import site
from django.test import TestCase

from company.models import Company
from invoices.admin import SalesInvoiceAdmin
from invoices.models import SalesInvoice
from invoices.resources import SalesInvoiceResource
from sales.models import Client


class SalesInvoiceListColumnsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(
            name="Acme", company=self.company, payment_terms_days=30,
        )

    def test_list_display_has_due_and_commitment_dates(self):
        admin = SalesInvoiceAdmin(SalesInvoice, site)
        self.assertIn("due_date", admin.list_display)
        self.assertIn("payment_commitment_date", admin.list_display)

    def test_export_includes_due_and_commitment_dates(self):
        invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 1, 15),
            payment_commitment_date=date(2026, 3, 1),
        )

        dataset = SalesInvoiceResource().export(
            queryset=SalesInvoice.objects.filter(pk=invoice.pk),
        )

        headers = list(dataset.headers)
        self.assertIn("due_date", headers)
        self.assertIn("payment_commitment_date", headers)
        row = dict(zip(headers, dataset[0]))
        self.assertEqual(self._parse(row["due_date"]), date(2026, 2, 14))
        self.assertEqual(self._parse(row["payment_commitment_date"]), date(2026, 3, 1))

    @staticmethod
    def _parse(value):
        """Export dates follow the project's DATE_INPUT_FORMATS (dd/mm/yyyy),
        so parse them back instead of asserting on a hardcoded format."""
        if isinstance(value, date):
            return value
        for fmt in [*settings.DATE_INPUT_FORMATS, "%Y-%m-%d"]:
            try:
                return datetime.strptime(str(value), fmt).date()
            except ValueError:
                continue
        raise AssertionError(f"Unparseable export date: {value!r}")
