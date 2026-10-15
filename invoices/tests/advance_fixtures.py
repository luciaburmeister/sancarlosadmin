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

Both build on ``ExpedienteInvoicingMixin``: the invoices, delivery orders
and wizard posts the tests make of ``self.project_file``. ``AdvanceApiMixin``
adds an authenticated mobile API client. The module-level readers return
an invoice's taxes and totals as plain tuples to compare.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from djmoney.money import Money
from rest_framework.test import APIClient

from apps.einvoicing.models import (
    EInvoicingAccount,
    EInvoicingSubmission,
    SubmissionAction,
    SubmissionStatus,
)
from company.models import Company, Employee
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
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.services.discounts import ADVANCE_DEDUCTION_PREFIX, ADVANCE_LINE_PREFIX
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile
from materials.models import PaymentMethod
from sales.models import (
    Client,
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    DiscountKind,
    DiscountLevel,
    DiscountMode,
    DiscountPolicy,
    DiscountRule,
    Quote,
    QuoteInventoryItem,
    QuoteStatus,
)

# The project-file invoice wizard's step field
WIZARD_STEP = "project_file_invoice_wizard_view-current_step"


def full_items(project_file):
    """Line dicts of "100 % del pendiente", as the API builds them."""
    summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file)
    ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(summary, "percentage", Decimal("100"))
    return ProjectFileInvoiceService.build_items_for_ratio(summary, ratio)


def _invoice_level_taxes(invoice):
    """The invoice-level AppliedTax rows of ``invoice``."""
    return AppliedTax.objects.filter(
        content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=invoice.pk,
    ).select_related("tax_type")


def invoice_taxes(invoice):
    """{(tax name, %): (base, amount)} of the invoice-level taxes."""
    return {
        (tax.tax_type.tax_name, tax.selected_percentage): (tax.base_amount.amount, tax.calculated_amount.amount)
        for tax in _invoice_level_taxes(invoice)
    }


def invoice_tax_rows(invoice):
    """[(tax name, %, base, amount, order)] of the invoice-level taxes, in order."""
    return [
        (tax.tax_type.tax_name, tax.selected_percentage, tax.base_amount.amount,
         tax.calculated_amount.amount, tax.order)
        for tax in _invoice_level_taxes(invoice).order_by("order", "pk")
    ]


def line_taxes(line):
    """[(tax name, %, base, amount)] of a line's taxes, in order."""
    return [
        (tax.tax_type.tax_name, tax.selected_percentage, tax.base_amount.amount,
         tax.calculated_amount.amount)
        for tax in line.applied_taxes.select_related("tax_type").order_by("order", "pk")
    ]


def totals(invoice):
    """(subtotal, discount, taxable base, tax, total) of ``invoice``, reloaded."""
    invoice.refresh_from_db()
    return (
        invoice.subtotal_amount.amount,
        invoice.discount_amount.amount,
        invoice.taxable_base.amount,
        invoice.tax_amount.amount,
        invoice.total_amount.amount,
    )


def aeat_cancel_marker(invoice, *, status=SubmissionStatus.PENDING):
    """A cancellation of ``invoice`` at the AEAT in ``status``, on its company's account."""
    account, _created = EInvoicingAccount.objects.get_or_create(
        company=invoice.company, name="Verifacti",
    )
    return EInvoicingSubmission.objects.create(
        account=account, sales_invoice=invoice, action=SubmissionAction.CANCEL, status=status,
    )


class ExpedienteInvoicingMixin:
    """Invoices, delivery orders and wizard posts of ``self.project_file``;
    needs ``company``, ``client_obj`` and ``project_file`` set up."""

    def summary(self, project_file=None, **kwargs):
        """The invoiceable summary of ``project_file`` (default: the expediente's)."""
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

    def rest_invoice(self, ratio=Decimal("1")):
        """A pro forma billing ``ratio`` of the goods, deducting the default plan."""
        invoice = self.invoice_goods(ratio)
        AdvanceDeductionService.apply(invoice)
        return invoice

    def issue_advance(self, *, percentage=Decimal("30"), number="INV-2026-00012"):
        """A ``percentage`` advance of 03/09/2026 marked FINAL as ``number``."""
        advance = AdvanceInvoiceService.create(
            self.project_file, date_issued=date(2026, 9, 3), percentage=percentage,
        )
        advance.status = SalesInvoiceStatus.FINAL
        advance.invoice_number = number
        advance.save(update_fields=["status", "invoice_number"])
        return advance

    def advance_and_rest(self):
        """The worked example: a 30 % advance issued, then "100 % del pendiente"."""
        advance = self.issue_advance()
        return advance, self.rest_invoice()

    def make_delivery_order(self, *, config, amount=Decimal("1000.00"), status=DeliveryOrderStatus.DRAFT):
        """A delivery order of one unit of ``config`` priced ``amount``."""
        order = DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, status=status,
        )
        DeliveryOrderItem.objects.create(
            delivery_order=order, description=config.name,
            quantity=Decimal("1"), unit_price=amount,
            source_content_type=ContentType.objects.get_for_model(Configuration),
            source_object_id=config.pk,
        )
        return order

    def invoice_delivery_orders(self, *orders):
        """The pro forma of ``orders``, dated 29/09/2026."""
        return DeliveryOrderService.create_invoice_from_delivery_orders(
            list(orders), company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )

    def login_to_wizard(self):
        """Log a superuser in; returns the expediente's invoice wizard URL."""
        self.client.force_login(User.objects.create_superuser("wizard", "w@x.com", "pw"))
        return reverse("admin:management_projectfile_create_invoice", args=[self.project_file.pk])

    def wizard_setup_data(self, **fields):
        """The wizard's setup-step POST: 100 % of the goods unless ``fields`` say otherwise."""
        data = {
            "company": self.company.pk,
            "date_issued": "2026-09-29",
            "due_date": "",
            "invoice_mode": "percentage",
            "percentage": "100",
            "custom_amount": "",
            "payment_method": PaymentMethod.TRANSFER,
            **fields,
        }
        return {WIZARD_STEP: "setup", **{f"setup-{name}": value for name, value in data.items()}}


class UserExpedienteMixin(ExpedienteInvoicingMixin):
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


class AdvanceExpedienteMixin(ExpedienteInvoicingMixin):
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


class AdvanceApiMixin(AdvanceExpedienteMixin):
    """``self.api``: a mobile API client logged in as an employee of the company."""

    api_username = "advances"

    def setUp(self):
        super().setUp()
        user = User.objects.create_user(self.api_username, password="pw")
        Employee.objects.create(
            user=user, company=self.company, first_name="Ana", last_name="Anticipo",
            is_active=True, hired_at=date(2020, 1, 1),
        )
        self.api = APIClient()
        self.api.force_authenticate(user=user)
