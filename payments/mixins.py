"""Mixin for any document that can receive payments (Invoice, SalesInvoice, etc.)."""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import DecimalField, F, OuterRef, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce


class Payable(models.Model):
    """
    Abstract mixin for documents that can be matched against bank transactions.

    Any model that mixes this in must implement:
    - amount_remaining (property returning Money or Decimal)

    Provides:
    - amount_remaining_decimal: always returns a plain Decimal, safe for arithmetic.
    - counterparty_key: hashable identifier for the counterparty (provider/client).
    """

    class Meta:
        abstract = True

    @property
    def amount_remaining_decimal(self) -> Decimal:
        """Return the outstanding balance as a plain Decimal (no Money wrapper)."""
        remaining = self.amount_remaining
        if hasattr(remaining, "amount"):
            return Decimal(str(remaining.amount))
        return Decimal(str(remaining))

    @property
    def counterparty_key(self) -> tuple:
        """
        Return a hashable key identifying this document's counterparty.

        Override in subclasses if the FK name differs from the defaults.
        Defaults: Invoice → ('provider', provider_id),
                  SalesInvoice → ('client', client_id).
        """
        if hasattr(self, "provider_id"):
            return ("provider", self.provider_id)
        if hasattr(self, "client_id"):
            return ("client", self.client_id)
        return ("unknown", self.pk)


_ZERO = Value(Decimal("0.00"))
_DECIMAL_FIELD = DecimalField(max_digits=12, decimal_places=2)


def annotate_outstanding_balance(
    queryset: QuerySet,
    invoice_model,
    counterparty_field: str,
    annotation_name: str = "outstanding_balance",
    invoice_filters: dict | None = None,
) -> QuerySet:
    """
    Annotate a Provider/Client queryset with the total outstanding balance
    (sum of `amount_remaining` across all not-fully-paid `invoice_model`
    rows pointing at each row via `counterparty_field`).

    `invoice_model` must be a `Payable` model with a `total_amount` MoneyField
    and a `payment_status` field (see `invoices.models.Invoice`/`SalesInvoice`).

    `invoice_filters` optionally narrows which invoices count, e.g.
    ``{"status": SalesInvoiceStatus.FINAL}`` to ignore proforma/cancelled ones.
    """
    from invoices.models import InvoicePaymentStatus
    from payments.models import PaymentAllocation

    ct_id = ContentType.objects.get_for_model(invoice_model).pk

    paid_subquery = Subquery(
        PaymentAllocation.objects.filter(
            content_type_id=ct_id,
            object_id=OuterRef("pk"),
        )
        .values("object_id")
        .annotate(total=Sum("amount_applied"))
        .values("total"),
        output_field=_DECIMAL_FIELD,
    )

    remaining_per_invoice = (
        invoice_model.objects.filter(
            **{counterparty_field: OuterRef("pk")}, **(invoice_filters or {})
        )
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .annotate(
            remaining=Coalesce(
                F("total_amount") - Coalesce(paid_subquery, _ZERO, output_field=_DECIMAL_FIELD),
                _ZERO,
                output_field=_DECIMAL_FIELD,
            )
        )
        .values(counterparty_field)
        .annotate(total_remaining=Sum("remaining"))
        .values("total_remaining")
    )

    return queryset.annotate(**{
        annotation_name: Coalesce(
            Subquery(remaining_per_invoice, output_field=_DECIMAL_FIELD),
            _ZERO,
            output_field=_DECIMAL_FIELD,
        )
    })
