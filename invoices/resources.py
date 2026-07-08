from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget
from .models import Invoice, SalesInvoice, InvoiceItem, SalesInvoiceItem, AppliedTax
from materials.models import Provider
from company.models import Company
from sales.models import Client
from django.utils.translation import gettext_lazy as _
from django.db.models import Sum, Max, DecimalField, Value
from django.db.models.functions import Coalesce
from django.db import models as models
from django.contrib.contenttypes.models import ContentType
from decimal import Decimal


# ── Shared tax-column helpers ─────────────────────────────────────────────────

MAX_IVA_SLOTS = 3  # columns: BASE IVA 1..3, IVA 1..3


def _bucket_taxes(tax_rows):
    """
    Given an iterable of AppliedTax objects (already with tax_type loaded),
    bucket them into (iva_list, retention_total).
    """
    iva_list = []
    retention_total = Decimal("0.00")

    for tax in tax_rows:
        base = tax.base_amount.amount if tax.base_amount else Decimal("0.00")
        amount = tax.calculated_amount.amount if tax.calculated_amount else Decimal("0.00")
        pct = tax.selected_percentage
        group = tax.tax_type.a3_tax_group  # IVA, IRPF, RE, NONE

        if group == "IVA":
            iva_list.append({"base": base, "amount": amount, "pct": pct})
        elif group == "IRPF" or tax.tax_type.behavior == "subtract":
            retention_total += abs(amount)

    return iva_list, retention_total


def _prefetch_tax_data(model_class, queryset):
    """
    Single bulk query: fetch ALL AppliedTax rows for every object in the
    queryset, then return a dict  {object_id: (iva_list, retention_total)}.

    This replaces N per-instance queries with exactly 1 query.
    """
    from collections import defaultdict

    ct = ContentType.objects.get_for_model(model_class)
    object_ids = list(queryset.values_list("pk", flat=True))

    if not object_ids:
        return {}

    all_taxes = (
        AppliedTax.objects
        .filter(content_type=ct, object_id__in=object_ids)
        .select_related("tax_type")
        .order_by("object_id", "order", "id")
    )

    # Group taxes by object_id
    taxes_by_obj = defaultdict(list)
    for tax in all_taxes:
        taxes_by_obj[tax.object_id].append(tax)

    # Bucket each group
    result = {}
    for obj_id in object_ids:
        result[obj_id] = _bucket_taxes(taxes_by_obj.get(obj_id, []))

    return result


def _build_tax_fields():
    """Generate the Field instances for tax columns."""
    tax_fields = {}
    for i in range(1, MAX_IVA_SLOTS + 1):
        tax_fields[f"base_iva_{i}"] = fields.Field(
            column_name=f"BASE IVA {i}", readonly=True
        )
        tax_fields[f"iva_{i}"] = fields.Field(
            column_name=f"IVA {i}", readonly=True
        )
    tax_fields["retencion"] = fields.Field(
        column_name="RETENCION", readonly=True
    )
    return tax_fields


# ── Invoice (Received / Purchase) ────────────────────────────────────────────

class InvoiceResource(resources.ModelResource):
    provider = fields.Field(
        column_name=_('Provider'),
        attribute='provider',
        widget=ForeignKeyWidget(Provider, 'name')
    )
    provider_tax_identifier = fields.Field(column_name=_('Provider NIF/CIF'))
    company = fields.Field(
        column_name=_('Company'),
        attribute='company',
        widget=ForeignKeyWidget(Company, 'name')
    )
    company_tax_identifier = fields.Field(column_name=_('Company NIF/CIF'))
    total_amount_value = fields.Field(column_name=_('Total Amount'))
    amount_paid_value = fields.Field(column_name=_('Amount Paid'))
    amount_remaining_value = fields.Field(column_name=_('Amount Remaining'))
    currency = fields.Field(column_name=_('Currency'))
    last_payment_date = fields.Field(column_name=_('Last Payment Date'))
    created_at = fields.Field(column_name=_('Created At'), attribute='created_at')

    # Tax columns
    base_iva_1 = fields.Field(column_name="BASE IVA 1")
    iva_1 = fields.Field(column_name="IVA 1")
    base_iva_2 = fields.Field(column_name="BASE IVA 2")
    iva_2 = fields.Field(column_name="IVA 2")
    base_iva_3 = fields.Field(column_name="BASE IVA 3")
    iva_3 = fields.Field(column_name="IVA 3")
    retencion = fields.Field(column_name="RETENCION")

    class Meta:
        model = Invoice
        fields = (
            'invoice_number',
            'date_issued',
            'due_date',
            'accounting_date',
            'provider',
            'provider_tax_identifier',
            'company',
            'company_tax_identifier',
            'payment_method',
            'total_amount_value',
            'amount_paid_value',
            'amount_remaining_value',
            'currency',
            'base_iva_1',
            'iva_1',
            'base_iva_2',
            'iva_2',
            'base_iva_3',
            'iva_3',
            'retencion',
            'confirmation_status',
            'payment_status',
            'last_payment_date',
            'ocr_processed',
            'is_total_balanced',
            'created_at',
        )
        export_order = fields

    def _annotate_queryset(self, queryset):
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(Invoice)
        paid_subquery = (
            PaymentAllocation.objects.filter(
                content_type=ct,
                object_id=models.OuterRef("pk"),
            )
            .values("object_id")
            .annotate(total=Sum("amount_applied"))
            .values("total")
        )
        last_payment_subquery = (
            PaymentAllocation.objects.filter(
                content_type=ct,
                object_id=models.OuterRef("pk"),
            )
            .values("object_id")
            .annotate(last_date=Max("payment__payment_date"))
            .values("last_date")
        )
        return queryset.select_related("provider", "company").annotate(
            _amount_paid=Coalesce(
                models.Subquery(paid_subquery),
                Value(Decimal("0.00")),
                output_field=DecimalField(),
            ),
            _last_payment_date=models.Subquery(last_payment_subquery),
        )

    def get_queryset(self):
        return self._annotate_queryset(Invoice.objects.all())

    def export(self, queryset=None, *args, **kwargs):
        if queryset is not None:
            queryset = self._annotate_queryset(queryset)
        else:
            queryset = self.get_queryset()

        # Bulk-prefetch all taxes in 1 query → attach to each instance
        tax_lookup = _prefetch_tax_data(Invoice, queryset)
        instances = list(queryset)
        for inst in instances:
            inst._tax_cache = tax_lookup.get(inst.pk, ([], Decimal("0.00")))

        return super().export(queryset=instances, *args, **kwargs)

    def dehydrate_total_amount_value(self, invoice):
        return invoice.total_amount.amount if invoice.total_amount else 0

    def dehydrate_amount_paid_value(self, invoice):
        return getattr(invoice, '_amount_paid', None) or 0

    def dehydrate_amount_remaining_value(self, invoice):
        total = invoice.total_amount.amount if invoice.total_amount else Decimal("0.00")
        paid = getattr(invoice, '_amount_paid', None) or Decimal("0.00")
        return total - paid

    def dehydrate_currency(self, invoice):
        return str(invoice.total_amount.currency) if invoice.total_amount else "EUR"

    def dehydrate_provider_tax_identifier(self, invoice):
        return invoice.provider.tax_identifier if invoice.provider and invoice.provider.tax_identifier else ""

    def dehydrate_company_tax_identifier(self, invoice):
        return invoice.company.tax_identifier if invoice.company and invoice.company.tax_identifier else ""

    # ── Tax dehydrators ───────────────────────────────────────────────────

    def _get_tax_data(self, invoice):
        """Read from pre-attached _tax_cache (set in export())."""
        return getattr(invoice, '_tax_cache', ([], Decimal("0.00")))

    def dehydrate_base_iva_1(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[0]["base"] if len(iva_list) > 0 else ""

    def dehydrate_iva_1(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[0]["amount"] if len(iva_list) > 0 else ""

    def dehydrate_base_iva_2(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[1]["base"] if len(iva_list) > 1 else ""

    def dehydrate_iva_2(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[1]["amount"] if len(iva_list) > 1 else ""

    def dehydrate_base_iva_3(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[2]["base"] if len(iva_list) > 2 else ""

    def dehydrate_iva_3(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[2]["amount"] if len(iva_list) > 2 else ""

    def dehydrate_retencion(self, invoice):
        _, retention = self._get_tax_data(invoice)
        return retention if retention else ""

    def dehydrate_last_payment_date(self, invoice):
        dt = getattr(invoice, '_last_payment_date', None)
        return dt.isoformat() if dt else ""


class InvoiceItemResource(resources.ModelResource):
    invoice_number = fields.Field(attribute='invoice__invoice_number', column_name=_('Invoice Number'))
    provider = fields.Field(attribute='invoice__provider__name', column_name=_('Provider'))
    company = fields.Field(attribute='invoice__company__name', column_name=_('Company'))
    unit_price_amount = fields.Field(column_name=_('Unit Price'))
    line_total_amount = fields.Field(column_name=_('Line Total'))
    currency = fields.Field(column_name=_('Currency'))

    class Meta:
        model = InvoiceItem
        fields = (
            'invoice_number',
            'provider',
            'company',
            'description',
            'quantity',
            'unit_price_amount',
            'line_total_amount',
            'currency',
            'account__name',
        )
        export_order = fields

    def dehydrate_unit_price_amount(self, item):
        return item.unit_price.amount if item.unit_price else 0

    def dehydrate_line_total_amount(self, item):
        return item.line_total.amount if item.line_total else 0

    def dehydrate_currency(self, item):
        return str(item.unit_price.currency) if item.unit_price else "EUR"


# ── Sales Invoice ─────────────────────────────────────────────────────────────

class SalesInvoiceResource(resources.ModelResource):
    client = fields.Field(
        column_name=_('Client'),
        attribute='client',
        widget=ForeignKeyWidget(Client, 'name')
    )
    client_tax_identifier = fields.Field(column_name=_('Client NIF/CIF'))
    company = fields.Field(
        column_name=_('Company'),
        attribute='company',
        widget=ForeignKeyWidget(Company, 'name')
    )
    company_tax_identifier = fields.Field(column_name=_('Company NIF/CIF'))
    subtotal_amount_value = fields.Field(column_name=_('Subtotal'))
    tax_amount_value = fields.Field(column_name=_('Tax Amount'))
    total_amount_value = fields.Field(column_name=_('Total Amount'))
    currency = fields.Field(column_name=_('Currency'))
    last_payment_date = fields.Field(column_name=_('Last Payment Date'))

    # Tax columns
    base_iva_1 = fields.Field(column_name="BASE IVA 1")
    iva_1 = fields.Field(column_name="IVA 1")
    base_iva_2 = fields.Field(column_name="BASE IVA 2")
    iva_2 = fields.Field(column_name="IVA 2")
    base_iva_3 = fields.Field(column_name="BASE IVA 3")
    iva_3 = fields.Field(column_name="IVA 3")
    retencion = fields.Field(column_name="RETENCION")

    class Meta:
        model = SalesInvoice
        fields = (
            'invoice_number',
            'incremental_id',
            'status',
            'date_issued',
            'due_date',
            'client',
            'client_tax_identifier',
            'company',
            'company_tax_identifier',
            'subtotal_amount_value',
            'tax_amount_value',
            'total_amount_value',
            'currency',
            'base_iva_1',
            'iva_1',
            'base_iva_2',
            'iva_2',
            'base_iva_3',
            'iva_3',
            'retencion',
            'payment_status',
            'last_payment_date',
            'payment_method',
            'reference',
        )
        export_order = fields

    def export(self, queryset=None, *args, **kwargs):
        from payments.models import PaymentAllocation

        if queryset is None:
            queryset = SalesInvoice.objects.all()

        ct = ContentType.objects.get_for_model(SalesInvoice)
        last_payment_subquery = (
            PaymentAllocation.objects.filter(
                content_type=ct,
                object_id=models.OuterRef("pk"),
            )
            .values("object_id")
            .annotate(last_date=Max("payment__payment_date"))
            .values("last_date")
        )
        queryset = queryset.select_related("client", "company").annotate(
            _last_payment_date=models.Subquery(last_payment_subquery),
        )

        # Bulk-prefetch all taxes in 1 query → attach to each instance
        tax_lookup = _prefetch_tax_data(SalesInvoice, queryset)
        instances = list(queryset)
        for inst in instances:
            inst._tax_cache = tax_lookup.get(inst.pk, ([], Decimal("0.00")))

        return super().export(queryset=instances, *args, **kwargs)

    def dehydrate_subtotal_amount_value(self, invoice):
        return invoice.subtotal_amount.amount if invoice.subtotal_amount else 0

    def dehydrate_tax_amount_value(self, invoice):
        return invoice.tax_amount.amount if invoice.tax_amount else 0

    def dehydrate_total_amount_value(self, invoice):
        return invoice.total_amount.amount if invoice.total_amount else 0

    def dehydrate_currency(self, invoice):
        return str(invoice.total_amount.currency) if invoice.total_amount else "EUR"

    def dehydrate_client_tax_identifier(self, invoice):
        return invoice.client.tax_identifier if invoice.client and invoice.client.tax_identifier else ""

    def dehydrate_company_tax_identifier(self, invoice):
        return invoice.company.tax_identifier if invoice.company and invoice.company.tax_identifier else ""

    # ── Tax dehydrators ───────────────────────────────────────────────────

    def _get_tax_data(self, invoice):
        """Read from pre-attached _tax_cache (set in export())."""
        return getattr(invoice, '_tax_cache', ([], Decimal("0.00")))

    def dehydrate_base_iva_1(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[0]["base"] if len(iva_list) > 0 else ""

    def dehydrate_iva_1(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[0]["amount"] if len(iva_list) > 0 else ""

    def dehydrate_base_iva_2(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[1]["base"] if len(iva_list) > 1 else ""

    def dehydrate_iva_2(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[1]["amount"] if len(iva_list) > 1 else ""

    def dehydrate_base_iva_3(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[2]["base"] if len(iva_list) > 2 else ""

    def dehydrate_iva_3(self, invoice):
        iva_list, _ = self._get_tax_data(invoice)
        return iva_list[2]["amount"] if len(iva_list) > 2 else ""

    def dehydrate_retencion(self, invoice):
        _, retention = self._get_tax_data(invoice)
        return retention if retention else ""

    def dehydrate_last_payment_date(self, invoice):
        dt = getattr(invoice, '_last_payment_date', None)
        return dt.isoformat() if dt else ""


class SalesInvoiceItemResource(resources.ModelResource):
    invoice_number = fields.Field(attribute='invoice__invoice_number', column_name=_('Invoice Number'))
    client = fields.Field(attribute='invoice__client__name', column_name=_('Client'))
    company = fields.Field(attribute='invoice__company__name', column_name=_('Company'))
    unit_price_amount = fields.Field(column_name=_('Unit Price'))
    line_total_amount = fields.Field(column_name=_('Line Total'))
    currency = fields.Field(column_name=_('Currency'))

    class Meta:
        model = SalesInvoiceItem
        fields = (
            'invoice_number',
            'client',
            'company',
            'description',
            'reference',
            'quantity',
            'unit_price_amount',
            'line_total_amount',
            'currency',
        )
        export_order = fields

    def dehydrate_unit_price_amount(self, item):
        return item.unit_price.amount if item.unit_price else 0

    def dehydrate_line_total_amount(self, item):
        return item.line_total.amount if item.line_total else 0

    def dehydrate_currency(self, item):
        return str(item.unit_price.currency) if item.unit_price else "EUR"
