"""OCR'd purchase invoices get a rounding line so lines + taxes match the printed total."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from djmoney.money import Money

from accounting.models import (
    AccountRole, ChartOfAccountsTemplate, TemplateAccount, CompanyAccount, CompanyChartOfAccounts, EntryStatus, FiscalYear,
)
from accounting.services.journal_generator import JournalGeneratorService
from invoices.models import Invoice, InvoiceItem, TaxType
from invoices.services.po_matching import POInvoiceMatchService
from invoices.tests.factories import apply_tax, get_iva21, make_company
from materials.models import Provider


class RoundingLineTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.rounding_account = CompanyAccount.objects.create(
            company=self.company, code="669000", name="Redondeos", role=AccountRole.ROUNDING,
        )
        self.expense_account = CompanyAccount.objects.create(
            company=self.company, code="600000", name="Compras",
        )
        template = ChartOfAccountsTemplate.objects.create(name="PGC", country="ES")
        t472 = TemplateAccount.objects.create(template=template, code="472", name="IVA soportado")
        CompanyAccount.objects.create(company=self.company, code="472000", name="IVA soportado", template_account=t472)
        TaxType.objects.filter(pk=get_iva21().pk).update(purchase_account=t472)
        self.chart = CompanyChartOfAccounts.objects.create(company=self.company, template=template)
        FiscalYear.objects.create(
            company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        self.provider = Provider.objects.create(
            company=self.company, name="Proveedor SA", tax_identifier="A11111111",
            account=CompanyAccount.objects.create(company=self.company, code="400001", name="Proveedor"),
        )

    def _invoice(self, total, *, lines=("33.33", "33.33", "33.33"), tax="21.00"):
        invoice = Invoice.objects.create(
            company=self.company, provider=self.provider, invoice_number="F-1",
            date_issued=date(2026, 10, 1), total_amount=Money(total, "EUR"),
        )
        for price in lines:
            InvoiceItem.objects.create(
                invoice=invoice, description="Perfil", quantity=1,
                unit_price=Money(price, "EUR"), account=self.expense_account,
            )
        apply_tax(invoice, tax_type=get_iva21(), base=sum(Decimal(p) for p in lines), calculated=Decimal(tax))
        return invoice

    def test_gap_becomes_rounding_line_on_configured_account(self):
        invoice = self._invoice("121.00")  # 99.99 + 21.00 = 120.99

        item = invoice.apply_rounding_line()

        self.assertEqual(item.line_total, Money("0.01", "EUR"))
        self.assertEqual(item.account, self.rounding_account)
        self.assertTrue(item.is_rounding)
        self.assertEqual(invoice.calculated_total(), Decimal("121.00"))

    def test_negative_gap(self):
        invoice = self._invoice("120.97")

        self.assertEqual(invoice.apply_rounding_line().line_total, Money("-0.02", "EUR"))

    def test_rerun_replaces_previous_rounding_line(self):
        invoice = self._invoice("121.00")
        invoice.apply_rounding_line()
        invoice.total_amount = Money("121.02", "EUR")

        invoice.apply_rounding_line()

        self.assertEqual(
            list(invoice.items.filter(is_rounding=True).values_list("unit_price", flat=True)),
            [Decimal("0.03")],
        )

    def test_balanced_invoice_gets_no_line(self):
        self.assertIsNone(self._invoice("120.99").apply_rounding_line())

    def test_gap_over_tolerance_is_left_for_review(self):
        invoice = self._invoice("125.00")

        self.assertIsNone(invoice.apply_rounding_line())
        self.assertFalse(invoice.items.filter(is_rounding=True).exists())

    def test_no_account_with_rounding_role(self):
        self.rounding_account.role = ""
        self.rounding_account.save()

        self.assertIsNone(self._invoice("121.00").apply_rounding_line())

    def test_no_chart_installed(self):
        self.chart.delete()

        self.assertIsNone(self._invoice("121.00").apply_rounding_line())

    def test_journal_entry_posts_with_rounding_line(self):
        invoice = self._invoice("121.00")
        invoice.apply_rounding_line()

        entry = JournalGeneratorService(self.company).journalize_invoice(invoice)

        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertTrue(entry.lines.filter(account=self.rounding_account, debit=Money("0.01", "EUR")).exists())

    def test_journal_entry_skips_zero_lines(self):
        invoice = self._invoice("121.00", lines=("100.00", "0.00"))

        entry = JournalGeneratorService(self.company).journalize_invoice(invoice)

        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertEqual(entry.lines.filter(account=self.expense_account).count(), 1)

    def test_po_matching_ignores_rounding_line(self):
        invoice = self._invoice("121.00")
        invoice.apply_rounding_line()

        items = [item for item, _ in POInvoiceMatchService.invoice_items_with_remaining(invoice)]

        self.assertEqual(len(items), 3)
        self.assertFalse(any(item.is_rounding for item in items))



class RoundingIntegrityTests(TestCase):
    """The guarantees hold in the database, not only through save()/clean()."""

    def setUp(self):
        self.company = make_company()
        self.account = CompanyAccount.objects.create(
            company=self.company, code="669000", name="Redondeos", role=AccountRole.ROUNDING,
        )

    def test_giving_the_role_to_another_account_moves_it(self):
        other = CompanyAccount.objects.create(
            company=self.company, code="669001", name="Otra", role=AccountRole.ROUNDING,
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.role, "")
        self.assertEqual(other.role, AccountRole.ROUNDING)

    def test_each_company_has_its_own_rounding_account(self):
        CompanyAccount.objects.create(
            company=make_company(name="Other"), code="669000", name="X", role=AccountRole.ROUNDING,
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.role, AccountRole.ROUNDING)

    def test_db_rejects_two_rounding_accounts(self):
        other = CompanyAccount.objects.create(company=self.company, code="669001", name="Otra")

        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyAccount.objects.filter(pk=other.pk).update(role=AccountRole.ROUNDING)

    def test_db_rejects_inactive_or_unpostable_rounding_account(self):
        for fields in ({"is_active": False}, {"is_postable": False}):
            with self.subTest(**fields), self.assertRaises(IntegrityError), transaction.atomic():
                CompanyAccount.objects.filter(pk=self.account.pk).update(**fields)

    def test_clean_explains_unpostable_role_account(self):
        self.account.is_postable = False

        with self.assertRaises(ValidationError):
            self.account.full_clean()

    def test_db_rejects_negative_tolerance(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyChartOfAccounts.objects.create(
                company=self.company,
                template=ChartOfAccountsTemplate.objects.create(name="PGC", country="ES"),
                rounding_tolerance=Decimal("-0.01"),
            )

    def test_db_rejects_two_rounding_lines_on_one_invoice(self):
        invoice = Invoice.objects.create(company=self.company, invoice_number="F-1", total_amount=Money("1", "EUR"))
        InvoiceItem.objects.create(invoice=invoice, description="R", unit_price=Money("0.01", "EUR"), is_rounding=True)

        with self.assertRaises(IntegrityError), transaction.atomic():
            InvoiceItem.objects.create(invoice=invoice, description="R", unit_price=Money("0.01", "EUR"), is_rounding=True)
