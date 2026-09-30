"""
Shared fixtures for the advance invoice tests.

``UserExpedienteMixin``: the user's expediente, 1,557.49 of quote inventory
lines at IVA 21 % with a 2 % financing rule on those line items.

``AdvanceExpedienteMixin``: an expediente whose accepted quote holds one
configuration of 1,000.00 at IVA 21 %, plus helpers that store advance
invoices and deduction lines straight through the ORM, the way the advance
services lay them out:

- an advance invoice (``document_type=advance``) has one line per VAT group
  (``advance_pf_<pf>``) carrying its own per-line taxes;
- a deduction line (``advance_ded_<line>``, ``advance_line`` set) is
  negative and lowers the invoice's matching tax base by its amount.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from djmoney.money import Money

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from inventory.models import InventoryItem
from invoices.models import (
    AppliedTax,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    TaxType,
)
from invoices.services.discounts import ADVANCE_DEDUCTION_PREFIX, ADVANCE_LINE_PREFIX
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile
from sales.models import (
    Client,
    DiscountKind,
    DiscountLevel,
    DiscountMode,
    DiscountPolicy,
    DiscountRule,
    Quote,
    QuoteInventoryItem,
    QuoteStatus,
)


class UserExpedienteMixin:
    """Mix into a TestCase: the user's expediente, 1,557.49 of line items at
    IVA 21 %, with a 2 % financing rule on those line items (accumulated):
    taxable base 1,526.34, IVA 320.53, total 1,846.87."""

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Anticipos SL")
        self.client_obj = Client.objects.create(name="Cliente SA", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="Obra", company=self.company, client=self.client_obj,
        )
        self.iva, _created = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )
        policy = DiscountPolicy.objects.create(name="Financiación", company=self.company)
        DiscountRule.objects.create(
            policy=policy, name="Financiación 2%", order=0, percentage=Decimal("2.00"),
            mode=DiscountMode.ACCUMULATED, level=DiscountLevel.LINE_ITEMS_BASKET,
            kind=DiscountKind.FINANCING,
        )
        self.quote = Quote.objects.create(
            project_name="Obra", company=self.company, client=self.client_obj,
            project_file=self.project_file, status=QuoteStatus.ACCEPTED,
        )
        self.quote.discount_policies.add(policy)
        self._inv_item("Ventana", Decimal("3"), Decimal("345.83"))   # 1,037.49
        self._inv_item("Puerta", Decimal("1"), Decimal("520.00"))
        self.quote.recalc_totals()
        self.quote.refresh_from_db()

    def _inv_item(self, name, quantity, unit_price):
        """A quote inventory line; it copies its stock item's IVA 21 % on creation."""
        stock_item = InventoryItem.objects.create(name=name, company=self.company)
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(InventoryItem),
            object_id=stock_item.pk,
            tax_type=self.iva,
            selected_percentage=Decimal("21"),
        )
        QuoteInventoryItem.objects.create(
            quote=self.quote, inventory_item=stock_item,
            quantity=quantity, unit_price=unit_price,
        )


class AdvanceExpedienteMixin:
    """Mix into a TestCase; ``setUp`` builds ``self.project_file``."""

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Anticipos SL")
        self.client_obj = Client.objects.create(name="Cliente SA", company=self.company)
        self.product = ProductTemplate.objects.create(name="Ventana", company=self.company)
        self.iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )
        self.project_file, self.quote, self.config = self.make_expediente("Obra")

    def make_expediente(self, name, subtotal=Decimal("1000.00")):
        """A project file with an accepted quote holding one IVA 21 % configuration."""
        project_file = ProjectFile.objects.create(
            name=name, company=self.company, client=self.client_obj,
        )
        quote = Quote.objects.create(
            project_name=name, company=self.company, client=self.client_obj,
            project_file=project_file, status=QuoteStatus.ACCEPTED,
        )
        config = self.add_config(quote, subtotal)
        return project_file, quote, config

    def add_config(self, quote, subtotal, percentage=Decimal("21")):
        """A configuration on ``quote`` taxed at ``percentage``; re-totals the quote."""
        tax = (subtotal * percentage / Decimal("100")).quantize(Decimal("0.01"))
        config = Configuration.objects.create(
            name=f"Configuración {subtotal}",
            company=self.company,
            product=self.product,
            client=self.client_obj,
            quote=quote,
            project_file=quote.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=subtotal,
            total_price=subtotal + tax,
            tax_breakdown=[{
                "tax_name": self.iva.tax_name, "percentage": str(percentage),
                "base": str(subtotal), "tax_amount": str(tax),
            }],
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=config.pk,
            tax_type=self.iva,
            selected_percentage=percentage,
            base_amount=subtotal,
            calculated_amount=tax,
        )
        quote.recalc_totals()
        quote.refresh_from_db()
        return config

    def summary(self, project_file=None, **kwargs):
        return ProjectFileInvoiceService.get_invoiceable_summary(
            project_file or self.project_file, **kwargs
        )

    def invoice_goods(self, ratio=Decimal("1"), project_file=None):
        """A pro forma billing ``ratio`` of every goods source of the expediente."""
        project_file = project_file or self.project_file
        return ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
            due_date=None,
            items_data=ProjectFileInvoiceService.build_items_for_ratio(
                self.summary(project_file), ratio,
            ),
        )

    def make_advance(self, base, percentage, status=SalesInvoiceStatus.FINAL,
                     tax_percentage=Decimal("21")):
        """An advance invoice of ``base`` (one VAT group). Returns (invoice, line)."""
        invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            document_type=SalesInvoiceDocumentType.ADVANCE,
            advance_percentage=Decimal(percentage),
            date_issued=date(2026, 9, 1),
        )
        line = SalesInvoiceItem.objects.create(
            invoice=invoice,
            description=f"Anticipo {percentage} %",
            quantity=Decimal("1"),
            unit_price=Money(base, "EUR"),
            source_id=f"{ADVANCE_LINE_PREFIX}{self.project_file.pk}",
        )
        for obj in (line, invoice):
            AppliedTax.objects.create(
                content_type=ContentType.objects.get_for_model(obj),
                object_id=obj.pk,
                tax_type=self.iva,
                selected_percentage=tax_percentage,
                base_amount=Money(base, "EUR"),
            )
        invoice.recalculate_totals()
        if status != SalesInvoiceStatus.PROFORMA:
            self.set_status(invoice, status)
        return invoice, line

    def deduct(self, invoice, advance_line, amount):
        """A line on the pro forma ``invoice`` deducting ``amount`` of ``advance_line``."""
        line = SalesInvoiceItem.objects.create(
            invoice=invoice,
            description="A deducir: anticipo",
            quantity=Decimal("1"),
            unit_price=Money(-amount, "EUR"),
            source_id=f"{ADVANCE_DEDUCTION_PREFIX}{advance_line.pk}",
            advance_line=advance_line,
            # Only bulk invoices tag their lines with the expediente.
            project_file=None if invoice.project_file_id else advance_line.invoice.project_file,
        )
        invoice_ct = ContentType.objects.get_for_model(invoice)
        for line_tax in advance_line.applied_taxes.all():
            tax = AppliedTax.objects.get(
                content_type=invoice_ct,
                object_id=invoice.pk,
                tax_type=line_tax.tax_type,
                selected_percentage=line_tax.selected_percentage,
            )
            tax.base_amount = Money(tax.base_amount.amount - amount, "EUR")
            tax.save(update_fields=["base_amount", "base_amount_currency"])
        invoice.recalculate_totals()
        return line

    def set_status(self, invoice, status):
        invoice.status = status
        invoice.save(update_fields=["status"])
