"""
Shared fixture builders for the invoicing, delivery-order and OCR tests.

- An expediente: company, client, product, project file, IVA, an accepted
  quote and configurations taxed through AppliedTax (``ExpedienteMixin`` puts
  the usual set on ``self``).
- Delivery orders created from those sources (``make_delivery_order``,
  ``do_line``), and the stock discounted quotes they are built from.
- ``OcrTestMixin``: purchase invoices processed by a canned ``FakeOcr``.
- The project-file wizard's and API's line builders.

Every value a test asserts on is passed explicitly or is a documented default.
"""
import copy
import tempfile
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from djmoney.money import Money

from company.models import BankAccount, Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from invoices.models import AppliedTax, Invoice, TaxType
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.services.invoice.invoice_service import InvoiceService
from invoices.services.ocr_services import InvoiceData, OcrInterface
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile
from materials.models import Provider
from sales.models import Client, Quote, QuoteStatus

# ── expediente ──────────────────────────────────────────────────────────


def make_company(*, name="Test Co", **fields):
    """A company."""
    return Company.objects.create(name=name, **fields)


def make_client(*, company, name="Acme"):
    """A client of ``company``."""
    return Client.objects.create(name=name, company=company)


def make_product_template(*, company, name="Door", **fields):
    """A product template of ``company``."""
    return ProductTemplate.objects.create(name=name, company=company, **fields)


def make_project_file(*, company, client, name="PF"):
    """An expediente for ``client``."""
    return ProjectFile.objects.create(name=name, company=company, client=client)


def make_provider(*, company, name="Perfiles SA", **fields):
    """A provider of ``company``."""
    return Provider.objects.create(name=name, company=company, **fields)


def make_bank_account(*, company, bank_name="Banco Uno", iban="ES0012345678", **fields):
    """A bank account of ``company``."""
    return BankAccount.objects.create(company=company, bank_name=bank_name, iban=iban, **fields)


def make_purchase_invoice(*, company, provider, total, number="P-1", date_issued=date(2026, 10, 1), **fields):
    """
    A provider's invoice of ``total`` with no lines. Saved once: saving it
    again recomputes the total from its (absent) lines.
    """
    return Invoice.objects.create(
        company=company, provider=provider, invoice_number=number, date_issued=date_issued,
        total_amount=Money(total, "EUR"), net_amount=Money(total, "EUR"), **fields,
    )


def make_ledger(*, company, start=date(2000, 1, 1), end=date(2100, 12, 31)):
    """A chart of accounts for ``company`` and an open fiscal year: its payments get journalized and posted."""
    from accounting.models import ChartOfAccountsTemplate, CompanyChartOfAccounts, FiscalYear

    template = ChartOfAccountsTemplate.objects.get_or_create(name="PGC", defaults={"country": "ES"})[0]
    CompanyChartOfAccounts.objects.create(company=company, template=template)
    FiscalYear.objects.create(company=company, name="FY", start_date=start, end_date=end)


def get_iva21():
    """The shared Spanish IVA tax type (an adding tax)."""
    return TaxType.objects.get_or_create(
        tax_name="IVA", country="ES", defaults={"behavior": "add"},
    )[0]


def get_irpf():
    """The shared Spanish IRPF tax type (a retention)."""
    return TaxType.objects.get_or_create(
        tax_name="IRPF", country="ES", defaults={"behavior": "subtract"},
    )[0]


def apply_tax(
    obj, *, tax_type, percentage="21", base=None, calculated=None, order=None,
):
    """An AppliedTax on ``obj``; ``calculated`` defaults to base × percentage."""
    fields = {}
    if base is not None:
        base = Decimal(base)
        if calculated is None:
            calculated = (base * Decimal(percentage) / 100).quantize(Decimal("0.01"))
        fields.update(base_amount=base, calculated_amount=calculated)
    if order is not None:
        fields["order"] = order
    return AppliedTax.objects.create(
        content_type=ContentType.objects.get_for_model(obj), object_id=obj.pk,
        tax_type=tax_type, selected_percentage=Decimal(percentage), **fields,
    )


def make_quote(
    *, project_file, project_name="Quote", status=QuoteStatus.ACCEPTED, **fields,
):
    """A quote on ``project_file``, accepted by default."""
    return Quote.objects.create(
        project_name=project_name, company=project_file.company,
        client=project_file.client, project_file=project_file, status=status, **fields,
    )


def make_configuration(
    *, product, quote=None, project_file=None, subtotal=None, total=None,
    tax_pct=21, name="Config", status=None, **fields,
):
    """A configuration of ``quote`` (direct if none), taxed at IVA ``tax_pct``."""
    project_file = project_file or quote.project_file
    if status is None:
        status = ConfigurationStatus.COMPLETED if quote else ConfigurationStatus.SHOP
    if subtotal is not None:
        subtotal = Decimal(subtotal)
        if total is None:
            rate = 1 + Decimal(tax_pct or 0) / 100
            total = (subtotal * rate).quantize(Decimal("0.01"))
        fields.update(subtotal_price=subtotal, total_price=total)
    config = Configuration.objects.create(
        name=name, company=project_file.company, product=product,
        client=project_file.client, quote=quote, project_file=project_file,
        status=status, **fields,
    )
    if tax_pct is not None:
        apply_tax(config, tax_type=get_iva21(), percentage=str(tax_pct), base=subtotal)
    return config


def create_invoice(*, project_file, items, date_issued, due_date=None):
    """A sales invoice built from ``project_file`` with ``items``."""
    return ProjectFileInvoiceService.create_invoice_from_project_file(
        project_file=project_file, company=project_file.company,
        client=project_file.client, date_issued=date_issued, due_date=due_date,
        items_data=items,
    )


class ExpedienteMixin:
    """setUp: company, client_obj, product, project_file, iva21, config_ct on self."""

    company_name = "Test Co"
    product_name = "Door"
    project_file_name = "PF"
    invoice_date_issued = date(2026, 8, 1)
    invoice_due_date = None

    def setUp(self):
        super().setUp()
        self.company = make_company(name=self.company_name)
        self.client_obj = make_client(company=self.company)
        self.product = make_product_template(
            company=self.company, name=self.product_name,
        )
        self.project_file = make_project_file(
            company=self.company, client=self.client_obj, name=self.project_file_name,
        )
        self.iva21 = get_iva21()
        self.config_ct = ContentType.objects.get_for_model(Configuration)

    def _make_config(self, quote, subtotal, **fields):
        """A configuration of ``quote`` (direct if None) on this expediente."""
        return make_configuration(
            quote=quote, project_file=self.project_file, product=self.product,
            subtotal=subtotal, **fields,
        )

    def _create_invoice(self, items_data):
        """An invoice of this expediente dated with the class's invoice dates."""
        return create_invoice(
            project_file=self.project_file, items=items_data,
            date_issued=self.invoice_date_issued, due_date=self.invoice_due_date,
        )

    def _create_do(self, items_data):
        """A delivery order of this expediente."""
        return make_delivery_order(project_file=self.project_file, items=items_data)


# ── discounted quotes and delivery orders ───────────────────────────────


def basket_discount(*, rule_name, percentage, amount, base="1000.00", kind="standard"):
    """One net basket entry of a quote's discount_breakdown."""
    return {
        "rule_name": rule_name, "kind": kind, "level": "basket", "mode": "net",
        "percentage": percentage, "base": base, "amount": amount,
    }


def discounted_quote(*, project_file, kind="standard", **fields):
    """1000 gross, one 10 % basket rule of ``kind``: 900 taxable, 189 IVA, 1089."""
    rule_name = "Financiación 10%" if kind == "financing" else "10%"
    values = dict(
        subtotal_price=Decimal("1000.00"), discount_amount=Decimal("100.00"),
        discounted_subtotal=Decimal("900.00"), tax_amount=Decimal("189.00"),
        total_price=Decimal("1089.00"),
        discount_breakdown=[basket_discount(
            rule_name=rule_name, kind=kind, percentage="10.00", amount="100.00",
        )],
    )
    return make_quote(project_file=project_file, **{**values, **fields})


def financed_quote(*, project_file, **fields):
    """1000 gross, 10 % standard + 5 % financing: 850 taxable, 178.50 IVA, 1028.50."""
    values = dict(
        subtotal_price=Decimal("1000.00"), discount_amount=Decimal("150.00"),
        discounted_subtotal=Decimal("850.00"), tax_amount=Decimal("178.50"),
        total_price=Decimal("1028.50"),
        discount_breakdown=[
            basket_discount(rule_name="10%", percentage="10.00", amount="100.00"),
            basket_discount(
                rule_name="Financiación 5%", kind="financing",
                percentage="5.00", amount="50.00",
            ),
        ],
    )
    return make_quote(project_file=project_file, **{**values, **fields})


def do_line(source, *, unit_price, quantity="1", discount="0.00"):
    """Delivery-order item data for a configuration or quote inventory line."""
    if isinstance(source, Configuration):
        description = source.name
    else:
        description = source.inventory_item.name
    return {
        "source_content_type_id": ContentType.objects.get_for_model(source).id,
        "source_object_id": source.pk,
        "description": description,
        "quantity": Decimal(quantity),
        "unit_price": Decimal(unit_price),
        "discount_percentage": Decimal(discount),
    }


def make_delivery_order(*, project_file, items):
    """A delivery order of ``project_file`` created through the service."""
    return DeliveryOrderService.create_delivery_order(
        project_file=project_file, company=project_file.company,
        client=project_file.client, items_data=items,
    )


# ── purchase-invoice OCR ────────────────────────────────────────────────

OCR_DEFAULTS = dict(
    document_type="invoice",
    vendor_name="Proveedor SA",
    vendor_tax_id="B12345678",
    company_tax_id="A87654321",
    invoice_number="F-001",
    date_issued="2026-01-15",
    due_date=None,
    net_amount=Decimal("100.00"),
    total_amount=Decimal("100.00"),
    currency="EUR",
    payment_method="transfer",
    line_items=[],
    taxes=[],
)


class FakeOcr(OcrInterface):
    """Returns a canned extraction instead of calling Gemini."""

    def __init__(self, **data):
        self.data = InvoiceData(**copy.deepcopy({**OCR_DEFAULTS, **data}))

    def extract_data(self, file_path: str) -> InvoiceData:
        return self.data


class OcrTestMixin:
    """A temp PDF at self.file_path and helpers running InvoiceService on a FakeOcr."""

    ocr_defaults = {}
    refresh_new_invoice = False

    def setUp(self):
        super().setUp()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as pdf:
            pdf.write(b"%PDF-1.4 fake invoice")
        self.file_path = pdf.name

    def fake_ocr(self, **overrides):
        """A FakeOcr with the class's defaults plus ``overrides``."""
        return FakeOcr(**{**self.ocr_defaults, **overrides})

    def _process_new(self, **ocr_overrides):
        """Pipeline A: OCR creates the invoice."""
        service = InvoiceService(ocr_processor=self.fake_ocr(**ocr_overrides))
        result = service.process_invoice(self.file_path)
        self.assertTrue(result.success, result.messages)
        if self.refresh_new_invoice:
            result.invoice.refresh_from_db()
        return result.invoice

    def _process_existing(self, invoice, **ocr_overrides):
        """Pipeline B: OCR updates an invoice that already exists."""
        service = InvoiceService(ocr_processor=self.fake_ocr(**ocr_overrides))
        invoice.file.name = "invoices/uploads/existing.pdf"
        invoice.save(update_fields=["file"])
        result = service.process_existing_invoice(invoice, file_path=self.file_path)
        self.assertTrue(result.success, result.messages)
        result.invoice.refresh_from_db()
        return result.invoice


# ── wizard and API line builders ────────────────────────────────────────


def build_wizard_items(summary, *, calculated_subtotal=None):
    """The project-file wizard's prefilled lines for 100 % of ``summary``."""
    from invoices.views.invoice.project_file_wizard_views import (
        ProjectFileInvoiceWizardView,
    )

    if calculated_subtotal is None:
        calculated_subtotal = summary["remaining_subtotal"]
    # model_admin is required by UnfoldModelAdminViewMixin; None is fine
    # because only _build_items_initial runs (no admin UI).
    view = ProjectFileInvoiceWizardView(model_admin=None)
    view.invoicing_summary = summary
    view.get_cleaned_data_for_step = lambda step: {
        "invoice_mode": "percentage",
        "percentage": Decimal("100"),
        "_calculated_subtotal": calculated_subtotal,
    }
    return view._build_items_initial()


def build_api_items(summary, *, percentage=Decimal("100")):
    """The mobile/API builder's lines for ``percentage`` of ``summary``."""
    from api.billing.serializers import build_items_from_summary

    return build_items_from_summary(summary, "percentage", percentage, None)
