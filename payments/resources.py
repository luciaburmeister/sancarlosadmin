from django.utils.translation import gettext_lazy as _
from import_export import fields, resources, widgets

from payments.models import Payment


def allocation_document_label(allocation):
    """The document an allocation settles, by the number people know it by."""
    document = allocation.related_object
    if document is None:
        return f"#{allocation.object_id}"
    number = getattr(document, "display_number", None) or getattr(document, "invoice_number", None)
    return number or str(document)


class PaymentResource(resources.ModelResource):
    """One row per payment, with its counterparty and the documents it settles."""

    id = fields.Field(attribute="id", column_name="ID", widget=widgets.IntegerWidget())
    payment_date = fields.Field(attribute="payment_date", column_name=_("Payment Date"), widget=widgets.DateWidget("%d/%m/%Y"))
    company = fields.Field(attribute="company__name", column_name=_("Company"))
    direction = fields.Field(column_name=_("Direction"))
    status = fields.Field(column_name=_("Status"))
    method = fields.Field(column_name=_("Method"))
    amount = fields.Field(column_name=_("Amount"))
    amount_pending_allocation = fields.Field(column_name=_("Amount Pending Allocation"))
    currency = fields.Field(column_name=_("Currency"))
    counterparty = fields.Field(column_name=_("Counterparty"))
    tax_identifier = fields.Field(column_name=_("Tax identifier"))
    reference = fields.Field(attribute="reference", column_name=_("Reference"))
    expected_bank_reference = fields.Field(attribute="expected_bank_reference", column_name=_("Expected bank reference"))
    bank_account = fields.Field(attribute="bank_account__iban", column_name=_("Bank Account"))
    documents = fields.Field(column_name=_("Documents"))
    journal_entry = fields.Field(attribute="live_entry_number", column_name=_("Journal entry"))
    created_by = fields.Field(column_name=_("Created By"))
    notes = fields.Field(attribute="notes", column_name=_("Notes"))

    COLUMN_FORMATS = {"amount": "#,##0.00", "amount_pending_allocation": "#,##0.00"}

    class Meta:
        model = Payment
        fields = (
            "id", "payment_date", "company", "direction", "status", "method", "amount",
            "amount_pending_allocation", "currency", "counterparty", "tax_identifier", "reference",
            "expected_bank_reference", "bank_account", "documents", "journal_entry", "created_by", "notes",
        )
        export_order = fields

    def _counterparty(self, payment):
        return payment.client or payment.provider

    def dehydrate_direction(self, payment):
        return payment.get_direction_display()

    def dehydrate_status(self, payment):
        return payment.get_status_display()

    def dehydrate_method(self, payment):
        return payment.get_method_display()

    def dehydrate_created_by(self, payment):
        return payment.get_created_by_display()

    def dehydrate_amount(self, payment):
        return payment.amount.amount

    def dehydrate_amount_pending_allocation(self, payment):
        return payment.amount_pending_allocation.amount

    def dehydrate_currency(self, payment):
        return str(payment.amount.currency)

    def dehydrate_counterparty(self, payment):
        counterparty = self._counterparty(payment)
        return counterparty.name if counterparty else ""

    def dehydrate_tax_identifier(self, payment):
        counterparty = self._counterparty(payment)
        return counterparty.tax_identifier if counterparty else ""

    def dehydrate_documents(self, payment):
        # Mirrors repeat a document the payment already settles through another
        return ", ".join(
            allocation_document_label(allocation)
            for allocation in payment.allocations.all()
            if not allocation.is_mirror
        )
