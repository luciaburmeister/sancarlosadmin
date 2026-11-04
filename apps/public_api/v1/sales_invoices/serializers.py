"""
Public contract for sales invoices (v1). Kept apart from the mobile app's
serializers in api/billing so a change there never changes this API.

Money is a decimal string with 2 places (``"121.00"``) next to ``currency``.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from invoices.models import (
    AppliedTax,
    InvoicePaymentStatus,
    PaymentMethod,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceStatus,
    TaxType,
)
from payments.models import PaymentAllocation

CENT = Decimal("0.01")


def _amount(value) -> str | None:
    """A Money/Decimal as a 2-place decimal string."""
    if value is None:
        return None
    amount = getattr(value, "amount", value)
    return str(Decimal(str(amount)).quantize(CENT))


class MoneyAmountField(serializers.DecimalField):
    """Read-only Money → ``"123.45"``."""

    def __init__(self, **kwargs):
        super().__init__(max_digits=14, decimal_places=2, read_only=True, **kwargs)

    def to_representation(self, value):
        if value is None:
            return None
        return super().to_representation(getattr(value, "amount", value))


_MONEY_SCHEMA = serializers.DecimalField(max_digits=14, decimal_places=2)


# ── read ────────────────────────────────────────────────────────────────


class ClientSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    tax_identifier = serializers.CharField(allow_null=True)


class SalesInvoiceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    invoice_number = serializers.CharField(allow_null=True, help_text="Set when the invoice is finalized.")
    proforma_number = serializers.CharField(allow_null=True)
    status = serializers.ChoiceField(choices=SalesInvoiceStatus.choices)
    document_type = serializers.ChoiceField(choices=SalesInvoiceDocumentType.choices)
    payment_status = serializers.ChoiceField(choices=InvoicePaymentStatus.choices)
    payment_method = serializers.CharField(allow_null=True)
    date_issued = serializers.DateField()
    due_date = serializers.DateField(allow_null=True)
    currency = serializers.CharField()
    reference = serializers.CharField(allow_null=True)
    client = ClientSummarySerializer(allow_null=True)
    subtotal = MoneyAmountField(source="subtotal_amount", help_text="Sum of the line totals.")
    discount = MoneyAmountField(source="discount_amount", allow_null=True)
    taxable_base = MoneyAmountField(help_text="Subtotal minus discount: what taxes apply to.")
    tax = MoneyAmountField(source="tax_amount", help_text="VAT minus retentions (IRPF).")
    total = MoneyAmountField(source="total_amount")
    amount_paid = serializers.SerializerMethodField()
    amount_remaining = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @staticmethod
    def _paid(invoice) -> Decimal:
        # The list annotates it (one query); a single invoice computes it
        if hasattr(invoice, "paid_total"):
            return invoice.paid_total or Decimal("0")
        return invoice.amount_paid.amount

    @extend_schema_field(_MONEY_SCHEMA)
    def get_amount_paid(self, invoice) -> str:
        return _amount(self._paid(invoice))

    @extend_schema_field(_MONEY_SCHEMA)
    def get_amount_remaining(self, invoice) -> str:
        total = invoice.total_amount.amount if invoice.total_amount else Decimal("0")
        return _amount(total - self._paid(invoice))


class SalesInvoiceLineSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    description = serializers.CharField()
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2)
    unit_price = MoneyAmountField()
    discount_percentage = serializers.DecimalField(max_digits=5, decimal_places=2)
    line_total = MoneyAmountField()


class SalesInvoiceTaxSerializer(serializers.Serializer):
    tax_type_id = serializers.IntegerField(help_text="See /api/v1/tax-types/.")
    name = serializers.CharField(source="tax_type.tax_name")
    rate = serializers.DecimalField(source="selected_percentage", max_digits=5, decimal_places=2)
    behavior = serializers.ChoiceField(
        source="tax_type.behavior",
        choices=[("add", "add"), ("subtract", "subtract")],
        help_text="`add` (VAT) adds to the total, `subtract` (IRPF retention) subtracts.",
    )
    base = MoneyAmountField(source="base_amount", allow_null=True)
    amount = MoneyAmountField(source="calculated_amount", allow_null=True)


class SalesInvoicePaymentSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="payment.id")
    date = serializers.DateField(source="payment.payment_date")
    amount = MoneyAmountField(source="amount_applied", help_text="Amount of the payment applied to this invoice.")
    method = serializers.CharField(source="payment.method", allow_null=True)
    reference = serializers.CharField(source="payment.reference", allow_null=True)


class SalesInvoiceDetailSerializer(SalesInvoiceSerializer):
    notes = serializers.CharField(allow_null=True)
    lines = serializers.SerializerMethodField()
    taxes = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()

    @extend_schema_field(SalesInvoiceLineSerializer(many=True))
    def get_lines(self, invoice):
        return SalesInvoiceLineSerializer(invoice.items.order_by("id"), many=True).data

    @extend_schema_field(SalesInvoiceTaxSerializer(many=True))
    def get_taxes(self, invoice):
        taxes = (
            AppliedTax.objects.filter(
                content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=invoice.pk,
            )
            .select_related("tax_type")
            .order_by("order", "id")
        )
        return SalesInvoiceTaxSerializer(taxes, many=True).data

    @extend_schema_field(SalesInvoicePaymentSerializer(many=True))
    def get_payments(self, invoice):
        allocations = (
            PaymentAllocation.objects.filter(
                content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=invoice.pk,
            )
            .select_related("payment")
            .order_by("payment__payment_date", "id")
        )
        return SalesInvoicePaymentSerializer(allocations, many=True).data


# ── query / write ───────────────────────────────────────────────────────


class SalesInvoiceFilterSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=SalesInvoiceStatus.choices, required=False)
    payment_status = serializers.ChoiceField(choices=InvoicePaymentStatus.choices, required=False)
    document_type = serializers.ChoiceField(choices=SalesInvoiceDocumentType.choices, required=False)
    client_id = serializers.IntegerField(required=False)
    date_from = serializers.DateField(required=False, help_text="Issued on or after (YYYY-MM-DD).")
    date_to = serializers.DateField(required=False, help_text="Issued on or before (YYYY-MM-DD).")
    search = serializers.CharField(
        required=False, help_text="Invoice or pro forma number, client name or tax id (contains).",
    )


class LineTaxSerializer(serializers.Serializer):
    tax_type_id = serializers.PrimaryKeyRelatedField(
        source="tax_type", queryset=TaxType.objects.filter(is_active=True),
        help_text="An active tax type from /api/v1/tax-types/.",
    )
    rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"),
        help_text="Percentage; one of the tax type's `rates` when it lists any.",
    )


class DraftLineSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=500)
    quantity = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=CENT)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    discount_percentage = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"), default=Decimal("0"),
    )
    taxes = LineTaxSerializer(
        many=True, required=False, default=list,
        help_text="Taxes on this line, e.g. VAT and a retention. Omit for an untaxed line.",
    )


class SalesInvoiceCreateSerializer(serializers.Serializer):
    client_id = serializers.IntegerField()
    date_issued = serializers.DateField(required=False, help_text="Defaults to today.")
    due_date = serializers.DateField(
        required=False, allow_null=True, help_text="Defaults to the client's payment terms.",
    )
    payment_method = serializers.ChoiceField(
        choices=PaymentMethod.choices, required=False, help_text="Defaults to the client's.",
    )
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    lines = DraftLineSerializer(many=True, allow_empty=False, max_length=500)

    def validate(self, data):
        date_issued, due_date = data.get("date_issued"), data.get("due_date")
        if date_issued and due_date and due_date < date_issued:
            raise serializers.ValidationError({"due_date": ["Cannot be before date_issued."]})
        return data


class SalesInvoiceFinalizeSerializer(serializers.Serializer):
    send_email = serializers.BooleanField(
        default=False,
        help_text="Email the final invoice (PDF) to the client's email address. Off unless asked for.",
    )


class FinalizeEmailSerializer(serializers.Serializer):
    queued = serializers.BooleanField(help_text="Whether the email was queued to be sent.")
    to = serializers.EmailField(allow_null=True)
    skip_reason = serializers.ChoiceField(
        choices=[("disabled", "disabled"), ("no_client_email", "no_client_email"), ("queue_failed", "queue_failed")],
        allow_null=True,
        help_text="Why it wasn't queued: not requested, the client has no email, or queueing failed.",
    )


class SalesInvoiceFinalizedSerializer(SalesInvoiceDetailSerializer):
    email = FinalizeEmailSerializer(source="_finalize_email")
