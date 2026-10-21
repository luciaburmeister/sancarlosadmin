"""Issued sales invoices are booked in the sales journal; cancelling one reverses it."""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from djmoney.money import Money

from accounting.models import (
    AccountRole, BusinessUnit, ChartOfAccountsTemplate, CompanyAccount, CompanyChartOfAccounts, EntryStatus,
    FiscalYear, JournalEntry, JournalType,
)
from accounting.models import IncomeType
from accounting.services.journal_generator import (
    MissingAccount, SalesJournalGeneratorService, UnsupportedCurrency,
)
from invoices.admin import SalesInvoiceBusinessUnitInline
from invoices.models import (
    AppliedTax, SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus, TaxType,
)
from invoices import line_sources
from invoices.services.sales_invoice_service import SalesInvoiceService
from company.models import Company
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin
from materials.models import Provider
from sales.models import Client


class SalesLedgerMixin(AdvanceExpedienteMixin):
    """The expediente's company with a chart, an open fiscal year and its sales accounts."""

    def setUp(self):
        super().setUp()
        from accounting.models import TemplateAccount

        template = ChartOfAccountsTemplate.objects.create(name="PGC", country="ES")
        CompanyChartOfAccounts.objects.create(company=self.company, template=template)
        self.t477 = TemplateAccount.objects.create(template=template, code="477", name="IVA repercutido")
        self.t473 = TemplateAccount.objects.create(template=template, code="473", name="Retenciones")
        self.fiscal_year = FiscalYear.objects.create(
            company=self.company, name="FY", start_date=date(2000, 1, 1), end_date=date(2100, 12, 31),
        )
        account = lambda code, name, role="": CompanyAccount.objects.create(  # noqa: E731
            company=self.company, code=code, name=name, role=role,
        )
        self.sales = account("700000", "Ventas de mercaderías", AccountRole.GOODS_SALES)
        self.services = account("705000", "Prestaciones de servicios", AccountRole.SERVICES_SALES)
        self.discounts = account("706000", "Descuentos sobre ventas", AccountRole.SALES_DISCOUNTS)
        self.goods_unit = BusinessUnit.objects.create(
            company=self.company, code="CARP", name="Carpintería", income_type=IncomeType.GOODS, is_default=True,
        )
        self.services_unit = BusinessUnit.objects.create(
            company=self.company, code="INST", name="Instalación", income_type=IncomeType.SERVICES,
        )
        self.output_vat = account("477000", "IVA repercutido")
        self.withheld = account("473000", "Retenciones")
        CompanyAccount.objects.filter(pk=self.output_vat.pk).update(template_account=self.t477)
        CompanyAccount.objects.filter(pk=self.withheld.pk).update(template_account=self.t473)
        TaxType.objects.filter(pk=self.iva.pk).update(sales_account=self.t477)
        self.irpf = TaxType.objects.get_or_create(tax_name="IRPF", country="ES", defaults={"behavior": "subtract"})[0]
        TaxType.objects.filter(pk=self.irpf.pk).update(sales_account=self.t473)
        self.rounding = account("669000", "Redondeos", AccountRole.ROUNDING)

    def proforma(self, *lines, discount="0", taxes=(("IVA", "21"),)):
        """A pro forma of ``lines``: a price (the default unit) or (price, business unit)."""
        invoice = SalesInvoice.objects.create(
            company=self.company, client=self.client_obj, discount_amount=Money(discount, "EUR"),
        )
        for line in lines:
            price, unit = line if isinstance(line, tuple) else (line, None)
            SalesInvoiceItem.objects.create(
                invoice=invoice, description="Ventana", quantity=1, unit_price=Money(price, "EUR"),
                business_unit=unit,
            )
        for name, percentage in taxes:
            tax_type = self.iva if name == "IVA" else TaxType.objects.get_or_create(
                tax_name=name, country="ES", defaults={"behavior": "subtract"},
            )[0]
            AppliedTax.objects.create(
                content_type=ContentType.objects.get_for_model(invoice), object_id=invoice.pk,
                tax_type=tax_type, selected_percentage=Decimal(percentage),
            )
        invoice.recalculate_totals()
        return invoice

    def issue(self, invoice):
        return SalesInvoiceService.finalize_invoice(invoice, send_email=False)

    def lines(self, entry):
        """{account code: (debit, credit)}"""
        return {
            line.account.code: (line.debit.amount, line.credit.amount)
            for line in entry.lines.select_related("account")
        }


class SalesJournalTests(SalesLedgerMixin, TestCase):
    def test_issuing_posts_the_entry(self):
        invoice = self.issue(self.proforma("100.00"))

        entry = invoice.journal_entries.get()
        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertEqual(entry.journal.type, JournalType.SALES)
        self.assertEqual(entry.date, invoice.date_issued)
        self.client_obj.refresh_from_db()
        client_account = self.client_obj.account
        self.assertEqual(client_account.code, "4300001")
        self.assertEqual(self.lines(entry), {
            client_account.code: (Decimal("121.00"), 0),
            "700000": (0, Decimal("100.00")),
            "477000": (0, Decimal("21.00")),
        })

    def test_header_discount_has_its_own_account(self):
        entry = self.issue(self.proforma("60.00", "40.00", discount="10.00")).journal_entries.get()

        lines = self.lines(entry)
        self.assertEqual(lines["700000"], (0, Decimal("100.00")))
        self.assertEqual(lines["706000"], (Decimal("10.00"), 0))
        self.assertEqual(lines["477000"], (0, Decimal("18.90")))
        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_discount_account_only_needed_with_a_discount(self):
        self.discounts.role = ""
        self.discounts.save()

        self.assertEqual(self.issue(self.proforma("100.00")).journal_entries.get().status, EntryStatus.POSTED)
        invoice = self.proforma("100.00", discount="5.00")
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        with self.assertRaisesMessage(MissingAccount, str(AccountRole.SALES_DISCOUNTS.label)):
            SalesJournalGeneratorService(self.company).journalize_invoice(invoice)

    def test_irpf_withheld_by_the_client_is_a_debit(self):
        entry = self.issue(self.proforma("100.00", taxes=(("IVA", "21"), ("IRPF", "15")))).journal_entries.get()

        lines = self.lines(entry)
        self.client_obj.refresh_from_db()
        self.assertEqual(lines["473000"], (Decimal("15.00"), 0))
        self.assertEqual(lines[self.client_obj.account.code], (Decimal("106.00"), 0))
        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_services_lines_go_to_services(self):
        entry = self.issue(self.proforma("100.00", ("50.00", self.services_unit))).journal_entries.get()

        lines = self.lines(entry)
        self.assertEqual(lines["700000"], (0, Decimal("100.00")))
        self.assertEqual(lines["705000"], (0, Decimal("50.00")))
        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_discount_on_mixed_invoice_leaves_income_whole(self):
        invoice = self.proforma("200.00", ("100.00", self.services_unit), discount="10.00")

        lines = self.lines(self.issue(invoice).journal_entries.get())

        self.assertEqual(lines["700000"], (0, Decimal("200.00")))
        self.assertEqual(lines["705000"], (0, Decimal("100.00")))
        self.assertEqual(lines["706000"], (Decimal("10.00"), 0))

    def test_missing_services_account_names_it(self):
        self.services.role = ""
        self.services.save()

        with self.assertRaisesMessage(MissingAccount, str(AccountRole.SERVICES_SALES.label)):
            invoice = self.proforma(("50.00", self.services_unit))
            self.set_status(invoice, SalesInvoiceStatus.FINAL)
            SalesJournalGeneratorService(self.company).journalize_invoice(invoice)

    def test_advance_is_an_ordinary_sale(self):
        advance, _line = self.make_advance(Decimal("300.00"), "30")

        entry = SalesJournalGeneratorService(self.company).journalize_invoice(advance)

        lines = self.lines(entry)
        self.assertEqual(lines["700000"], (0, Decimal("300.00")))
        self.assertEqual(lines["477000"], (0, Decimal("63.00")))
        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_deduction_takes_the_advance_back_off_sales(self):
        _advance, invoice = self.advance_and_rest()

        entry = self.issue(invoice).journal_entries.get()

        deducted = -sum(i.line_total.amount for i in invoice.items.filter(advance_line__isnull=False))
        goods = sum(i.line_total.amount for i in invoice.items.filter(advance_line__isnull=True))
        self.assertGreater(deducted, 0)
        lines = self.lines(entry)
        self.assertEqual(lines["700000"], (0, goods - deducted))
        if invoice.discount_amount.amount:
            self.assertEqual(lines["706000"], (invoice.discount_amount.amount, 0))
        self.assertEqual(lines["477000"], (0, invoice.tax_amount.amount))
        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_deduction_follows_its_advance_line_type(self):
        advance, invoice = self.advance_and_rest()
        advance_line = advance.items.get()
        advance_line.business_unit = self.services_unit
        advance_line.save(update_fields=["business_unit"])

        entry = self.issue(invoice).journal_entries.get()

        deducted = -sum(i.line_total.amount for i in invoice.items.filter(advance_line__isnull=False))
        self.assertEqual(self.lines(entry)["705000"], (deducted, 0))

    def test_stored_total_off_by_cents_goes_to_rounding(self):
        invoice = self.proforma("100.00")
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        SalesInvoice.objects.filter(pk=invoice.pk).update(total_amount=Decimal("121.01"), invoice_number="X-1")
        invoice.refresh_from_db()

        entry = SalesJournalGeneratorService(self.company).journalize_invoice(invoice)

        self.assertEqual(self.lines(entry)["669000"], (0, Decimal("0.01")))
        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_gap_over_tolerance_leaves_a_draft(self):
        invoice = self.proforma("100.00")
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        SalesInvoice.objects.filter(pk=invoice.pk).update(total_amount=Decimal("125.00"), invoice_number="X-1")
        invoice.refresh_from_db()

        entry = SalesJournalGeneratorService(self.company).journalize_invoice(invoice)

        self.assertNotIn("669000", self.lines(entry))
        self.assertEqual(entry.status, EntryStatus.DRAFT)

    def test_journalizing_twice_keeps_one_entry(self):
        invoice = self.issue(self.proforma("100.00"))

        self.assertIsNone(SalesJournalGeneratorService(self.company).journalize_invoice(invoice))
        self.assertEqual(invoice.journal_entries.count(), 1)

    def test_proforma_is_not_journalized(self):
        self.assertIsNone(SalesJournalGeneratorService(self.company).journalize_invoice(self.proforma("100.00")))

    def test_missing_sales_account_never_blocks_issuing(self):
        self.sales.role = ""
        self.sales.save()

        invoice = self.issue(self.proforma("100.00"))

        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertFalse(invoice.journal_entries.exists())

    def test_ledger_refusing_to_post_leaves_a_draft(self):
        self.fiscal_year.delete()

        invoice = self.issue(self.proforma("100.00"))

        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(invoice.journal_entries.get().status, EntryStatus.DRAFT)


class SalesJournalCancellationTests(SalesLedgerMixin, TestCase):
    def test_cancelling_reverses_the_posted_entry(self):
        invoice = self.issue(self.proforma("100.00"))
        original = invoice.journal_entries.get()

        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)

        reversal = JournalEntry.objects.get(reversal_of=original)
        self.assertEqual(reversal.status, EntryStatus.POSTED)
        self.assertEqual(reversal.sales_invoice, invoice)
        self.assertEqual(
            self.lines(reversal),
            {code: (credit, debit) for code, (debit, credit) in self.lines(original).items()},
        )
        original.refresh_from_db()
        self.assertEqual(original.status, EntryStatus.POSTED)

    def test_reversing_twice_creates_one_reversal(self):
        invoice = self.issue(self.proforma("100.00"))
        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)

        self.assertEqual(SalesJournalGeneratorService(self.company).reverse_invoice_entries(invoice), [])
        self.assertEqual(invoice.journal_entries.count(), 2)

    def test_cancelling_deletes_a_draft_entry(self):
        self.fiscal_year.delete()
        invoice = self.issue(self.proforma("100.00"))

        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)

        self.assertFalse(invoice.journal_entries.exists())

    def test_cancelling_a_proforma_books_nothing(self):
        invoice = self.proforma("100.00")

        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)

        self.assertFalse(JournalEntry.objects.filter(sales_invoice=invoice).exists())


class PartnerAccountNumberingTests(SalesLedgerMixin, TestCase):
    """Client and provider accounts: the chart's root + the next number in the company."""

    def account_of(self, partner, root_field="client_account_root"):
        return SalesJournalGeneratorService(self.company)._get_partner_account(partner, root_field)

    def test_consecutive_per_company(self):
        other = Client.objects.create(name="Otro SA", company=self.company)

        self.assertEqual(self.account_of(self.client_obj).code, "4300001")
        self.assertEqual(self.account_of(other).code, "4300002")

    def test_existing_account_is_kept(self):
        first = self.account_of(self.client_obj)

        self.assertEqual(self.account_of(self.client_obj), first)
        self.assertEqual(CompanyAccount.objects.filter(code__startswith="430").count(), 1)

    def test_continues_after_existing_codes(self):
        CompanyAccount.objects.create(company=self.company, code="4300178", name="Antiguo")
        CompanyAccount.objects.create(company=self.company, code="43000999", name="Otra longitud")

        self.assertEqual(self.account_of(self.client_obj).code, "4300179")

    def test_other_companies_do_not_count(self):
        other_company = Company.objects.create(name="Otra SL")
        CompanyAccount.objects.create(company=other_company, code="4300005", name="Ajeno")

        self.assertEqual(self.account_of(self.client_obj).code, "4300001")

    def test_root_and_length_come_from_the_chart(self):
        CompanyChartOfAccounts.objects.filter(company=self.company).update(
            client_account_root="411", partner_account_length=8,
        )

        self.assertEqual(self.account_of(self.client_obj).code, "41100001")

    def test_providers_use_their_own_root(self):
        provider = Provider.objects.create(company=self.company, name="Proveedor SA", tax_identifier="A1")

        self.assertEqual(self.account_of(provider, "provider_account_root").code, "4000001")
        provider.refresh_from_db()
        self.assertEqual(provider.account.code, "4000001")

    def test_chart_rejects_unusable_roots(self):
        chart = CompanyChartOfAccounts.objects.get(company=self.company)
        for root in ("43A", "4300000"):
            chart.client_account_root = root
            with self.subTest(root=root), self.assertRaises(ValidationError):
                chart.full_clean()


class SalesJournalRetryTests(SalesLedgerMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.sales.role = ""
        self.sales.save()
        self.invoice = self.issue(self.proforma("100.00"))

    def test_retry_after_fixing_the_account(self):
        with self.assertRaises(MissingAccount):
            SalesJournalGeneratorService(self.company).retry_invoice(self.invoice)

        self.sales.role = AccountRole.GOODS_SALES
        self.sales.save()
        entry = SalesJournalGeneratorService(self.company).retry_invoice(self.invoice)

        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_retry_rebuilds_a_draft(self):
        self.sales.role = AccountRole.GOODS_SALES
        self.sales.save()
        self.fiscal_year.delete()
        draft = SalesJournalGeneratorService(self.company).retry_invoice(self.invoice)
        FiscalYear.objects.create(
            company=self.company, name="FY", start_date=date(2000, 1, 1), end_date=date(2100, 12, 31),
        )

        entry = SalesJournalGeneratorService(self.company).retry_invoice(self.invoice)

        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertFalse(JournalEntry.objects.filter(pk=draft.pk).exists())
        self.assertEqual(self.invoice.journal_entries.count(), 1)

    def test_retry_leaves_a_posted_entry_alone(self):
        self.sales.role = AccountRole.GOODS_SALES
        self.sales.save()
        posted = SalesJournalGeneratorService(self.company).retry_invoice(self.invoice)

        self.assertIsNone(SalesJournalGeneratorService(self.company).retry_invoice(self.invoice))
        self.assertEqual(list(self.invoice.journal_entries.all()), [posted])

    def _press(self):
        self.client.force_login(User.objects.create_superuser("ledger", "l@x.com", "pw"))
        response = self.client.get(
            reverse("admin:invoices_salesinvoice_journalize_detail_action", args=[self.invoice.pk])
        )
        self.assertRedirects(
            response, reverse("admin:invoices_salesinvoice_change", args=[self.invoice.pk]),
            fetch_redirect_response=False,
        )
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_button_explains_a_missing_account(self):
        messages = self._press()

        self.assertEqual(len(messages), 1)
        self.assertIn(str(AccountRole.GOODS_SALES.label), messages[0])
        self.assertFalse(self.invoice.journal_entries.exists())

    def test_button_posts_once_fixed(self):
        self.sales.role = AccountRole.GOODS_SALES
        self.sales.save()

        self._press()

        self.assertEqual(self.invoice.journal_entries.get().status, EntryStatus.POSTED)


class TemplateRoleInstallTests(TestCase):
    """Roles set on the template reach the company's accounts when its chart is installed."""

    def setUp(self):
        from accounting.models import TemplateAccount

        self.template = ChartOfAccountsTemplate.objects.create(name="PGC", country="ES")
        add = lambda code, role="": TemplateAccount.objects.create(  # noqa: E731
            template=self.template, code=code, name=code, role=role,
        )
        add("700", AccountRole.GOODS_SALES)
        add("705", AccountRole.SERVICES_SALES)
        add("759")
        self.company = Company.objects.create(name="Nueva SL")
        CompanyChartOfAccounts.objects.create(company=self.company, template=self.template)

    def install(self):
        from accounting.services.chart_installer import install_chart_standard

        return install_chart_standard(self.company, self.template)

    def roles(self):
        return dict(
            CompanyAccount.objects.filter(company=self.company).exclude(role="").values_list("role", "code")
        )

    def test_new_company_gets_the_template_roles(self):
        self.install()

        self.assertEqual(self.roles(), {AccountRole.GOODS_SALES: "700", AccountRole.SERVICES_SALES: "705"})

    def test_reinstalling_fills_roles_on_existing_accounts(self):
        CompanyAccount.objects.create(company=self.company, code="700", name="Ventas")

        self.assertEqual(self.install(), 2)
        self.assertEqual(self.roles()[AccountRole.GOODS_SALES], "700")

    def test_a_role_chosen_by_hand_is_kept(self):
        CompanyAccount.objects.create(company=self.company, code="759", name="Otros", role=AccountRole.GOODS_SALES)

        self.install()

        self.assertEqual(self.roles()[AccountRole.GOODS_SALES], "759")

    def test_inactive_account_is_not_given_a_role(self):
        CompanyAccount.objects.create(company=self.company, code="700", name="Ventas", is_active=False)

        self.install()

        self.assertNotIn(AccountRole.GOODS_SALES, self.roles())

    def test_one_account_per_role_in_a_template(self):
        from accounting.models import TemplateAccount

        with self.assertRaises(IntegrityError), transaction.atomic():
            TemplateAccount.objects.filter(template=self.template, code="759").update(role=AccountRole.GOODS_SALES)


class BusinessUnitEditingTests(SalesLedgerMixin, TestCase):
    """An issued invoice's lines may change business unit until its entry is posted."""

    def setUp(self):
        super().setUp()
        self.fiscal_year.delete()  # issued with a draft entry
        self.invoice = self.issue(self.proforma("100.00"))
        self.line = self.invoice.items.get()

    def change_type(self):
        line = SalesInvoiceItem.objects.get(pk=self.line.pk)
        line.business_unit = self.services_unit
        line.save(update_fields=["business_unit"])

    def test_editable_while_the_entry_is_a_draft_and_rebuilt_on_retry(self):
        self.change_type()
        FiscalYear.objects.create(
            company=self.company, name="FY", start_date=date(2000, 1, 1), end_date=date(2100, 12, 31),
        )

        entry = SalesJournalGeneratorService(self.company).retry_invoice(self.invoice)

        self.assertEqual(self.lines(entry)["705000"], (0, Decimal("100.00")))
        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_locked_once_posted(self):
        FiscalYear.objects.create(
            company=self.company, name="FY", start_date=date(2000, 1, 1), end_date=date(2100, 12, 31),
        )
        SalesJournalGeneratorService(self.company).retry_invoice(self.invoice)

        with self.assertRaises(ValidationError):
            self.change_type()

    def test_locked_on_a_cancelled_invoice(self):
        self.set_status(self.invoice, SalesInvoiceStatus.CANCELLED)

        with self.assertRaises(ValidationError):
            self.change_type()

    def test_the_rest_of_an_issued_line_stays_frozen(self):
        line = SalesInvoiceItem.objects.get(pk=self.line.pk)
        line.unit_price = Money("1.00", "EUR")

        with self.assertRaises(ValidationError):
            line.save()
        with self.assertRaises(ValidationError):
            line.save(update_fields=["business_unit", "unit_price"])

    def test_admin_shows_the_tab_on_issued_invoices_only(self):
        from django.contrib import admin

        model_admin = admin.site._registry[SalesInvoice]
        request = type("Request", (), {"user": User.objects.create_superuser("tab", "t@x.com", "pw")})()

        self.assertIn(SalesInvoiceBusinessUnitInline, model_admin.get_inlines(request, self.invoice))
        self.assertNotIn(SalesInvoiceBusinessUnitInline, model_admin.get_inlines(request, self.proforma("1.00")))


class AdvanceBusinessUnitLockTests(SalesLedgerMixin, TestCase):
    def test_advance_line_locks_once_a_deduction_is_posted(self):
        self.fiscal_year.delete()
        advance, invoice = self.advance_and_rest()
        SalesJournalGeneratorService(self.company).journalize_invoice(advance)  # stays a draft
        FiscalYear.objects.create(
            company=self.company, name="FY", start_date=date(2000, 1, 1), end_date=date(2100, 12, 31),
        )
        self.issue(invoice)  # the deduction is posted as goods

        line = advance.items.get()
        line.business_unit = self.services_unit
        with self.assertRaises(ValidationError):
            line.save(update_fields=["business_unit"])


class BusinessUnitResolutionTests(SalesLedgerMixin, TestCase):
    """Lines take their unit from the product or inventory item they bill, else the company default."""

    def line(self, source_id):
        invoice = SalesInvoice.objects.create(company=self.company, client=self.client_obj)
        return SalesInvoiceItem.objects.create(
            invoice=invoice, description="L", quantity=1, unit_price=Money("10.00", "EUR"), source_id=source_id,
        )

    def test_configuration_line_takes_its_products_unit(self):
        self.product.business_unit = self.services_unit
        self.product.save(update_fields=["business_unit"])

        line = self.line(line_sources.source_id(line_sources.CONFIG, self.config.pk))

        self.assertEqual(line.business_unit, self.services_unit)

    def test_inventory_line_takes_its_items_unit(self):
        from inventory.choices import ItemType
        from inventory.models import InventoryItem
        from sales.models import QuoteInventoryItem

        item = InventoryItem.objects.create(
            company=self.company, name="Montaje", item_type=ItemType.SERVICE, business_unit=self.services_unit,
        )
        quote_item = QuoteInventoryItem.objects.create(
            quote=self.quote, inventory_item=item, quantity=1, unit_price=Money("10.00", "EUR"),
        )

        line = self.line(line_sources.source_id(line_sources.INV_ITEM, quote_item.pk))

        self.assertEqual(line.business_unit, self.services_unit)

    def test_product_without_unit_falls_back_to_the_default(self):
        line = self.line(line_sources.source_id(line_sources.CONFIG, self.config.pk))

        self.assertEqual(line.business_unit, self.goods_unit)

    def test_manual_line_takes_the_default(self):
        self.assertEqual(self.line("").business_unit, self.goods_unit)

    def test_no_unit_and_no_default_names_the_line(self):
        BusinessUnit.objects.filter(pk=self.goods_unit.pk).update(is_default=False)
        invoice = self.proforma("100.00")
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        SalesInvoiceItem.objects.filter(invoice=invoice).update(business_unit=None)

        with self.assertRaisesMessage(MissingAccount, "Ventana"):
            SalesJournalGeneratorService(self.company).journalize_invoice(invoice)

    def test_unit_of_another_company_is_refused(self):
        other = BusinessUnit.objects.create(company=Company.objects.create(name="Otra SL"), code="X", name="X")
        invoice = SalesInvoice.objects.create(company=self.company, client=self.client_obj)

        with self.assertRaises(ValidationError):
            SalesInvoiceItem.objects.create(
                invoice=invoice, description="L", quantity=1, unit_price=Money("1.00", "EUR"), business_unit=other,
            )
        self.product.business_unit = other
        with self.assertRaises(ValidationError):
            self.product.full_clean()


class BusinessUnitIntegrityTests(SalesLedgerMixin, TestCase):
    def test_becoming_default_moves_the_flag(self):
        self.services_unit.is_default = True
        self.services_unit.save()

        self.goods_unit.refresh_from_db()
        self.assertFalse(self.goods_unit.is_default)

    def test_db_rejects_two_defaults_or_an_inactive_default(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            BusinessUnit.objects.filter(pk=self.services_unit.pk).update(is_default=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BusinessUnit.objects.filter(pk=self.goods_unit.pk).update(is_active=False)

    def test_income_type_can_change_until_a_posted_entry_uses_it(self):
        self.goods_unit.income_type = IncomeType.SERVICES
        self.goods_unit.save()
        self.goods_unit.income_type = IncomeType.GOODS
        self.goods_unit.save()

        self.issue(self.proforma("100.00"))

        self.goods_unit.income_type = IncomeType.SERVICES
        with self.assertRaises(ValidationError):
            self.goods_unit.save()


class BusinessUnitCodeTests(TestCase):
    def test_codes_are_numbered_per_company(self):
        first, second = Company.objects.create(name="A SL"), Company.objects.create(name="B SL")
        unit = lambda company: BusinessUnit.objects.create(company=company, name="U")  # noqa: E731

        self.assertEqual([unit(first).code, unit(first).code], ["UN-001", "UN-002"])
        self.assertEqual(unit(second).code, "UN-001")

    def test_continues_after_existing_codes_and_keeps_manual_ones(self):
        company = Company.objects.create(name="A SL")
        BusinessUnit.objects.create(company=company, name="Antigua", code="CARP")
        BusinessUnit.objects.create(company=company, name="Importada", code="UN-007")

        self.assertEqual(BusinessUnit.objects.create(company=company, name="Nueva").code, "UN-008")


class TaxAndCurrencyGuardTests(SalesLedgerMixin, TestCase):
    def final_invoice(self, **fields):
        invoice = self.proforma("100.00")
        SalesInvoice.objects.filter(pk=invoice.pk).update(status=SalesInvoiceStatus.FINAL, invoice_number="X-1", **fields)
        invoice.refresh_from_db()
        return invoice

    def test_unmapped_tax_is_named(self):
        TaxType.objects.filter(pk=self.iva.pk).update(sales_account=None)

        with self.assertRaisesMessage(MissingAccount, "IVA"):
            SalesJournalGeneratorService(self.company).journalize_invoice(self.final_invoice())

    def test_tax_mapped_to_an_account_the_company_lacks(self):
        CompanyAccount.objects.filter(pk=self.output_vat.pk).update(template_account=None)

        with self.assertRaisesMessage(MissingAccount, "477"):
            SalesJournalGeneratorService(self.company).journalize_invoice(self.final_invoice())

    def test_no_guessing_by_code(self):
        TaxType.objects.filter(pk=self.iva.pk).update(sales_account=None)
        CompanyAccount.objects.create(company=self.company, code="4770001", name="Otra 477")

        with self.assertRaises(MissingAccount):
            SalesJournalGeneratorService(self.company).journalize_invoice(self.final_invoice())

    def test_exempt_tax_needs_no_account(self):
        exempt = TaxType.objects.create(tax_name="Exento", country="ES", behavior="add")
        invoice = self.proforma("100.00", taxes=())
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(invoice), object_id=invoice.pk,
            tax_type=exempt, selected_percentage=Decimal("0"),
        )
        invoice.recalculate_totals()

        self.assertEqual(self.issue(invoice).journal_entries.get().status, EntryStatus.POSTED)

    def test_foreign_currency_is_refused(self):
        invoice = self.final_invoice(currency="USD")

        with self.assertRaisesMessage(UnsupportedCurrency, "USD"):
            SalesJournalGeneratorService(self.company).journalize_invoice(invoice)
        self.assertFalse(invoice.journal_entries.exists())

    def test_foreign_currency_still_issues(self):
        invoice = self.proforma("100.00")
        SalesInvoice.objects.filter(pk=invoice.pk).update(currency="USD")
        invoice.refresh_from_db()

        invoice = self.issue(invoice)

        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertFalse(invoice.journal_entries.exists())


class AccountingSetupCheckTests(SalesLedgerMixin, TestCase):
    def statuses(self):
        from django.utils import translation

        from accounting.services.setup_check import AccountingSetupCheck

        with translation.override("en"):
            return {
                (section.title, check.label): check.status
                for section in AccountingSetupCheck(self.company).run()
                for check in section.checks
            }

    def test_configured_company_is_ready(self):
        from accounting.services.setup_check import AccountingSetupCheck

        CompanyAccount.objects.filter(pk=self.sales.pk).update(template_account=None)
        CompanyAccount.objects.create(
            company=self.company, code="7000001", name="Instalada",
            template_account=self.t477,  # marks the chart as installed
        )
        self.issue(self.proforma("100.00"))

        self.assertTrue(AccountingSetupCheck(self.company).is_ready())

    def test_reports_what_is_missing(self):
        from accounting.services.setup_check import MISSING, WARNING

        FiscalYear.objects.filter(company=self.company).delete()
        BusinessUnit.objects.filter(company=self.company).update(is_default=False)
        self.rounding.role = ""
        self.rounding.save()
        invoice = self.proforma("100.00")
        TaxType.objects.filter(pk=self.iva.pk).update(sales_account=None)

        statuses = self.statuses()

        self.assertEqual(statuses[("Fiscal year", "Current fiscal year")], MISSING)
        self.assertEqual(statuses[("Business units", "Default unit")], MISSING)
        self.assertEqual(statuses[("Account roles", "Rounding")], WARNING)
        self.assertEqual(statuses[("Taxes", "IVA · sales")], MISSING)
        self.assertTrue(invoice.pk)

    def test_services_role_is_required_once_a_unit_sells_services(self):
        from accounting.services.setup_check import MISSING

        self.services.role = ""
        self.services.save()

        self.assertEqual(self.statuses()[("Account roles", "Sales of services")], MISSING)

    def test_page_renders_for_the_company(self):
        self.client.force_login(User.objects.create_superuser("setup", "s@x.com", "pw"))

        response = self.client.get(reverse("admin:company_company_accounting_setup", args=[self.company.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.company.name)


class PostedEntryDeletionTests(SalesLedgerMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.entry = self.issue(self.proforma("100.00")).journal_entries.get()
        self.assertEqual(self.entry.status, EntryStatus.POSTED)

    def test_model_refuses_without_the_override(self):
        with self.assertRaises(ValidationError):
            self.entry.delete()
        self.entry.delete(allow_posted=True)
        self.assertFalse(JournalEntry.objects.filter(pk=self.entry.pk).exists())

    def test_superuser_deletes_from_the_admin(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.com", "pw"))

        response = self.client.post(
            reverse("admin:accounting_journalentry_delete", args=[self.entry.pk]), {"post": "yes"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(JournalEntry.objects.filter(pk=self.entry.pk).exists())

    def test_staff_cannot_delete_a_posted_entry(self):
        from django.contrib.auth.models import Permission

        from company.models import CompanyUser

        staff = User.objects.create_user("staff", "s@x.com", "pw", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="accounting", codename__in=["view_journalentry", "delete_journalentry"],
        ))
        CompanyUser.objects.create(user=staff, company=self.company)
        self.client.force_login(staff)

        self.client.post(reverse("admin:accounting_journalentry_delete", args=[self.entry.pk]), {"post": "yes"})
        self.client.post(reverse("admin:accounting_journalentry_changelist"), {
            "action": "delete_selected", "_selected_action": [self.entry.pk], "post": "yes",
        })

        self.assertTrue(JournalEntry.objects.filter(pk=self.entry.pk).exists())

        # ...while a draft goes, so the refusal above is the posted rule, not missing access
        draft = JournalEntry.objects.create(
            company=self.company, journal=self.entry.journal, date=self.entry.date, status=EntryStatus.DRAFT,
        )
        self.client.post(reverse("admin:accounting_journalentry_delete", args=[draft.pk]), {"post": "yes"})
        self.assertFalse(JournalEntry.objects.filter(pk=draft.pk).exists())


class PostingActionTests(SalesLedgerMixin, TestCase):
    def draft(self, day, *, balanced=True):
        entry = JournalEntry.objects.create(
            company=self.company, journal=SalesJournalGeneratorService(self.company)._get_sales_journal(),
            date=date(2026, 3, day), reference=f"R{day}", status=EntryStatus.DRAFT,
        )
        from accounting.models import JournalEntryLine

        JournalEntryLine.objects.create(company=self.company, entry=entry, account=self.sales,
                                        description="x", credit=Money("10.00", "EUR"))
        JournalEntryLine.objects.create(company=self.company, entry=entry, account=self.output_vat,
                                        description="x", debit=Money("10.00" if balanced else "9.00", "EUR"))
        return entry

    def login(self, superuser=True):
        user = (User.objects.create_superuser("poster", "p@x.com", "pw") if superuser
                else User.objects.create_user("viewer", "v@x.com", "pw", is_staff=True))
        self.client.force_login(user)
        return user

    def bulk_post(self, *entries):
        return self.client.post(reverse("admin:accounting_journalentry_changelist"), {
            "action": "post_selected", "_selected_action": [e.pk for e in entries],
        }, follow=True)

    def test_posts_drafts_in_date_order_and_reports_failures(self):
        self.login()
        later, earlier, broken = self.draft(20), self.draft(5), self.draft(10, balanced=False)

        response = self.bulk_post(later, earlier, broken)

        for entry in (later, earlier, broken):
            entry.refresh_from_db()
        self.assertEqual((earlier.status, later.status, broken.status),
                         (EntryStatus.POSTED, EntryStatus.POSTED, EntryStatus.DRAFT))
        self.assertLess(earlier.number, later.number)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("R10" in m for m in messages))

    def test_posted_entries_are_left_alone(self):
        self.login()
        entry = self.draft(5)
        entry.post()
        number = entry.number

        self.bulk_post(entry)

        entry.refresh_from_db()
        self.assertEqual(entry.number, number)

    def test_detail_button_posts_one(self):
        self.login()
        entry = self.draft(5)

        self.client.get(reverse("admin:accounting_journalentry_post_detail_action", args=[entry.pk]))

        entry.refresh_from_db()
        self.assertEqual(entry.status, EntryStatus.POSTED)

    def test_needs_change_permission(self):
        from django.contrib.auth.models import Permission

        from company.models import CompanyUser

        user = self.login(superuser=False)
        user.user_permissions.add(Permission.objects.get(
            content_type__app_label="accounting", codename="view_journalentry",
        ))
        CompanyUser.objects.create(user=user, company=self.company)
        entry = self.draft(5)

        self.bulk_post(entry)
        self.client.get(reverse("admin:accounting_journalentry_post_detail_action", args=[entry.pk]))

        entry.refresh_from_db()
        self.assertEqual(entry.status, EntryStatus.DRAFT)


class SalesInvoiceBulkJournalizeTests(SalesLedgerMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("bulk", "b@x.com", "pw"))

    def bulk(self, *invoices):
        return self.client.post(reverse("admin:invoices_salesinvoice_changelist"), {
            "action": "journalize_selected", "_selected_action": [i.pk for i in invoices],
        }, follow=True)

    def test_journalizes_the_selection_and_reports_each_outcome(self):
        # Issued while the sales account had no role: no entries yet
        self.sales.role = ""
        self.sales.save()
        self.services.role = ""
        self.services.save()
        first, second = self.issue(self.proforma("100.00")), self.issue(self.proforma("50.00"))
        services_only = self.issue(self.proforma(("80.00", self.services_unit)))  # still fails below
        self.sales.role = AccountRole.GOODS_SALES
        self.sales.save()
        already = self.issue(self.proforma("10.00"))
        proforma = self.proforma("5.00")

        response = self.bulk(first, second, services_only, already, proforma)

        self.assertEqual(first.journal_entries.get().status, EntryStatus.POSTED)
        self.assertEqual(second.journal_entries.get().status, EntryStatus.POSTED)
        self.assertFalse(services_only.journal_entries.exists())
        self.assertEqual(already.journal_entries.count(), 1)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn(services_only.display_number, text)

    def test_entry_numbers_follow_invoice_dates(self):
        self.sales.role = ""
        self.sales.save()
        later, earlier = self.issue(self.proforma("100.00")), self.issue(self.proforma("50.00"))
        SalesInvoice.objects.filter(pk=later.pk).update(date_issued=date(2026, 6, 2))
        SalesInvoice.objects.filter(pk=earlier.pk).update(date_issued=date(2026, 6, 1))
        self.sales.role = AccountRole.GOODS_SALES
        self.sales.save()

        self.bulk(later, earlier)

        self.assertLess(earlier.journal_entries.get().number, later.journal_entries.get().number)
