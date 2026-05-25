from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget
from .models import Invoice, SalesInvoice, InvoiceItem, SalesInvoiceItem
from materials.models import Provider
from company.models import Company
from sales.models import Client
from django.utils.translation import gettext_lazy as _

class InvoiceResource(resources.ModelResource):
    provider = fields.Field(
        column_name=_('Provider'),
        attribute='provider',
        widget=ForeignKeyWidget(Provider, 'name')
    )
    company = fields.Field(
        column_name=_('Company'),
        attribute='company',
        widget=ForeignKeyWidget(Company, 'name')
    )
    total_amount_value = fields.Field(column_name=_('Total Amount'))
    currency = fields.Field(column_name=_('Currency'))

    class Meta:
        model = Invoice
        fields = (
            'invoice_number',
            'date_issued',
            'due_date',
            'provider',
            'company',
            'payment_method',
            'total_amount_value',
            'currency',
            'confirmation_status',
            'payment_status',
            'ocr_processed',
            'is_total_balanced',
        )
        export_order = fields

    def dehydrate_total_amount_value(self, invoice):
        return invoice.total_amount.amount if invoice.total_amount else 0

    def dehydrate_currency(self, invoice):
        return str(invoice.total_amount.currency) if invoice.total_amount else "EUR"

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

class SalesInvoiceResource(resources.ModelResource):
    client = fields.Field(
        column_name=_('Client'),
        attribute='client',
        widget=ForeignKeyWidget(Client, 'name')
    )
    company = fields.Field(
        column_name=_('Company'),
        attribute='company',
        widget=ForeignKeyWidget(Company, 'name')
    )
    subtotal_amount_value = fields.Field(column_name=_('Subtotal'))
    tax_amount_value = fields.Field(column_name=_('Tax Amount'))
    total_amount_value = fields.Field(column_name=_('Total Amount'))
    currency = fields.Field(column_name=_('Currency'))

    class Meta:
        model = SalesInvoice
        fields = (
            'invoice_number',
            'incremental_id',
            'status',
            'date_issued',
            'due_date',
            'client',
            'company',
            'subtotal_amount_value',
            'tax_amount_value',
            'total_amount_value',
            'currency',
            'payment_status',
            'payment_method',
            'reference',
        )
        export_order = fields

    def dehydrate_subtotal_amount_value(self, invoice):
        return invoice.subtotal_amount.amount if invoice.subtotal_amount else 0

    def dehydrate_tax_amount_value(self, invoice):
        return invoice.tax_amount.amount if invoice.tax_amount else 0

    def dehydrate_total_amount_value(self, invoice):
        return invoice.total_amount.amount if invoice.total_amount else 0

    def dehydrate_currency(self, invoice):
        return str(invoice.total_amount.currency) if invoice.total_amount else "EUR"

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
