from datetime import date
from decimal import Decimal

from django.test import TestCase
from djmoney.money import Money

from accounting.models import CompanyAccount
from accounting.services.a3_exporter import A3PurchaseExportService
from company.models import Company
from invoices.models import A3TaxGroup, AppliedTax, Invoice, InvoiceItem, TaxType
from materials.models import Provider


class A3PurchaseExportVATBaseTest(TestCase):
    """
    Regression test: in 'Facturas Recibidas', each VAT row must carry the base
    that specific VAT rate was applied on, not the invoice's total base.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Test Company", tax_identifier="B12345678")
        self.provider_account = CompanyAccount.objects.create(
            company=self.company, code="400001", name="Proveedor", is_active=True
        )
        self.expense_account = CompanyAccount.objects.create(
            company=self.company, code="600000", name="Compras", is_active=True
        )
        self.provider = Provider.objects.create(
            company=self.company,
            name="Proveedor SA",
            tax_identifier="A11111111",
            account=self.provider_account,
        )
        self.vat = TaxType.objects.create(
            tax_name="IVA",
            country="ES",
            a3_tax_group=A3TaxGroup.IVA,
            percentage_options=[0, 4, 10, 21],
        )

        self.invoice = Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="F-2026-001",
            date_issued=date(2026, 6, 15),
            total_amount=Money("352.00", "EUR"),
        )
        InvoiceItem.objects.create(
            invoice=self.invoice,
            description="Material al 10%",
            quantity=1,
            unit_price=Money("100.00", "EUR"),
            account=self.expense_account,
        )
        InvoiceItem.objects.create(
            invoice=self.invoice,
            description="Material al 21%",
            quantity=1,
            unit_price=Money("200.00", "EUR"),
            account=self.expense_account,
        )

    def _apply_vat(self, pct, base=None, quota=None):
        AppliedTax.objects.create(
            content_object=self.invoice,
            tax_type=self.vat,
            selected_percentage=Decimal(pct),
            base_amount=Money(base, "EUR") if base is not None else None,
            calculated_amount=Money(quota, "EUR") if quota is not None else None,
        )

    def _export_rows(self):
        """Returns (pct, base, quota) per row of the 'Facturas Recibidas' sheet."""
        service = A3PurchaseExportService(
            self.company, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
        )
        sheet = service._get_recibidas_sheet()
        base_idx = sheet.headers.index("Base")
        pct_idx = sheet.headers.index("% IVA")
        quota_idx = sheet.headers.index("Cuota IVA")
        return sorted((row[pct_idx], row[base_idx], row[quota_idx]) for row in sheet)

    def test_each_vat_row_uses_its_own_base(self):
        self._apply_vat("10", base="100.00", quota="10.00")
        self._apply_vat("21", base="200.00", quota="42.00")
        self.assertEqual(
            self._export_rows(),
            [(10.0, 100.0, 10.0), (21.0, 200.0, 42.0)],
        )

    def test_base_derived_from_quota_when_stored_base_missing(self):
        self._apply_vat("10", quota="10.00")
        self._apply_vat("21", quota="42.00")
        self.assertEqual(
            self._export_rows(),
            [(10.0, 100.0, 10.0), (21.0, 200.0, 42.0)],
        )

    def test_vat_without_base_or_quota_falls_back_to_invoice_base(self):
        self._apply_vat("21")
        self.assertEqual(self._export_rows(), [(21.0, 300.0, 0.0)])
