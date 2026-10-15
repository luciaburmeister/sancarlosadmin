"""Mixin for any document that can receive payments (Invoice, SalesInvoice, etc.)."""
from decimal import Decimal
from functools import lru_cache

from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import (
    BooleanField,
    Case,
    DecimalField,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class PaidVia(models.TextChoices):
    """
    How a Payable-derived document got marked as paid: a manual user
    action, or automatically because it was matched to a bank movement
    during reconciliation.
    """
    MANUAL = "manual", _("Manual")
    RECONCILIATION = "reconciliation", _("Reconciliation")


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


@lru_cache(maxsize=1)
def _currency_symbols() -> dict[str, str]:
    """{code: symbol}, e.g. {"USD": "$"}, parsed from settings.CURRENCY_CHOICES."""
    return {
        code: label.removeprefix(f"{code} ")
        for code, label in django_settings.CURRENCY_CHOICES
    }


def currency_symbol(currency: str) -> str:
    """The display symbol for a currency code (settings.CURRENCY_CHOICES), e.g. "USD" -> "$"."""
    return _currency_symbols().get(currency, currency)


def annotate_outstanding_balance(
    queryset: QuerySet,
    invoice_model,
    counterparty_field: str,
    annotation_name: str = "outstanding_balance",
    invoice_filters: dict | None = None,
    invoice_q: Q | None = None,
) -> QuerySet:
    """
    Annotate a Provider/Client queryset with the total outstanding balance
    (sum of `amount_remaining` across all not-fully-paid `invoice_model`
    rows pointing at each row via `counterparty_field`).

    `invoice_model` must be a `Payable` model with a `total_amount` MoneyField
    and a `payment_status` field (see `invoices.models.Invoice`/`SalesInvoice`).

    `invoice_filters` optionally narrows which invoices count, e.g.
    ``{"status": SalesInvoiceStatus.FINAL}`` to ignore proforma/cancelled ones.
    `invoice_q` narrows them further with a `Q`, for conditions a dict of
    lookups can't express (see `annotate_split_outstanding_balance`).
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

    invoices = invoice_model.objects.filter(
        **{counterparty_field: OuterRef("pk")}, **(invoice_filters or {})
    ).exclude(payment_status=InvoicePaymentStatus.PAID)
    if invoice_q is not None:
        invoices = invoices.filter(invoice_q)

    remaining_per_invoice = (
        invoices
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


def annotate_balance_currency(
    queryset: QuerySet,
    invoice_model,
    counterparty_field: str,
    invoice_filters: dict | None = None,
    annotation_name: str = "balance_currency",
) -> QuerySet:
    """
    Annotate a Provider/Client queryset with the currency of its outstanding
    balance, read off one of the invoices `annotate_outstanding_balance` sums
    (they're expected to share one; this only exists so the total is labelled
    with what it's actually in instead of assuming EUR). Falls back to EUR for
    a row with no outstanding invoices, where the balance is zero anyway.
    """
    from invoices.models import InvoicePaymentStatus

    currency_subquery = Subquery(
        invoice_model.objects.filter(
            **{counterparty_field: OuterRef("pk")}, **(invoice_filters or {})
        )
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .order_by("pk")
        .values("total_amount_currency")[:1]
    )
    return queryset.annotate(**{
        annotation_name: Coalesce(currency_subquery, Value("EUR"))
    })


def annotate_effective_date(queryset: QuerySet, annotation_name: str = "effective_date") -> QuerySet:
    """
    Annotate an Invoice queryset with the date that actually drives its
    cash-flow forecasting: commitment_date if the provider agreed to a
    different payment date, else due_date, else date_issued -- so an
    invoice with no due_date at all still lands somewhere on a calendar
    instead of disappearing from it.

    Shared by PaymentsForecastService and InvoiceAdmin (its "View Invoices"
    drill-down) so a bucket's total and its link always agree on the same
    invoices.
    """
    return queryset.annotate(**{
        annotation_name: Coalesce("commitment_date", "due_date", "date_issued")
    })


def annotate_forecast_date(queryset: QuerySet, annotation_name: str = "forecast_date") -> QuerySet:
    """
    Annotate a SalesInvoice queryset with the date that actually drives its
    collections forecasting: payment_commitment_date if the client agreed
    to a different payment date, else due_date, else date_issued.

    SalesInvoice's equivalent of annotate_effective_date -- same idea, but
    against its own field names (payment_commitment_date, not
    commitment_date). Shared by CollectionsForecastService and
    SalesInvoiceAdmin (its "View Invoices" drill-down) so a bucket's total
    and its link always agree on the same invoices.
    """
    return queryset.annotate(**{
        annotation_name: Coalesce("payment_commitment_date", "due_date", "date_issued")
    })


def overdue_q(today=None) -> Q:
    """
    What makes an unpaid invoice overdue: a due date strictly before today, or
    no due date at all - an invoice nobody put a term on is treated as already
    due rather than indefinitely current.

    Kept identical to `invoices.filters.DueDatePassedFilter`, so the same
    invoice can't be overdue on a counterparty list and current on the invoice
    list its figures link to.
    """
    return Q(due_date__lt=today or timezone.localdate()) | Q(due_date__isnull=True)


def is_overdue(invoice, today=None) -> bool:
    """
    `overdue_q` for an invoice already in memory, for the callers that walk
    rows in Python instead of filtering in the database. Same rule - keep the
    two in step.
    """
    due_date = getattr(invoice, "due_date", None)
    return due_date is None or due_date < (today or timezone.localdate())


def annotate_split_outstanding_balance(
    queryset: QuerySet,
    invoice_model,
    counterparty_field: str,
    invoice_filters: dict | None = None,
    overdue_name: str = "balance_overdue",
    not_due_name: str = "balance_not_due",
) -> QuerySet:
    """
    Annotate a Provider/Client queryset with its outstanding balance split in
    two: what is already late (`overdue_name`) and what is still within terms
    (`not_due_name`). They partition the same invoices, so the two always add
    up to `annotate_outstanding_balance` with the same `invoice_filters`.
    """
    overdue = overdue_q()

    queryset = annotate_outstanding_balance(
        queryset, invoice_model, counterparty_field, overdue_name,
        invoice_filters=invoice_filters, invoice_q=overdue,
    )
    return annotate_outstanding_balance(
        queryset, invoice_model, counterparty_field, not_due_name,
        invoice_filters=invoice_filters, invoice_q=~overdue,
    )


def pending_invoices_queryset(invoice_model, invoice_filters: dict | None = None) -> QuerySet:
    """
    The not-fully-paid invoices behind an outstanding balance, annotated with
    `db_amount_remaining` and `is_overdue`, due date first (undated last).

    Exactly the invoices `annotate_outstanding_balance` sums with the same
    `invoice_filters`, so a balance and its breakdown can't disagree.
    """
    from invoices.models import InvoicePaymentStatus

    invoices = invoice_model.objects.filter(**(invoice_filters or {})).exclude(
        payment_status=InvoicePaymentStatus.PAID
    )
    return (
        annotate_amount_remaining(invoices, invoice_model)
        .annotate(
            # Case/When, not a bare Q: a Q alone yields NULL, never False.
            is_overdue=Case(
                When(overdue_q(), then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
        .order_by(F("due_date").asc(nulls_last=True), "pk")
    )


def unallocated_payments_q(direction=None) -> Q:
    """
    What makes a payment count towards a counterparty's credit: money that
    actually arrived (not draft, not cancelled) and has not yet been applied
    to any document. `direction` narrows to inbound or outbound.
    """
    from payments.models import PaymentStatus

    q = Q(amount_pending_allocation__gt=0) & ~Q(
        status__in=[PaymentStatus.DRAFT, PaymentStatus.CANCELLED]
    )
    if direction is not None:
        q &= Q(direction=direction)
    return q


def annotate_credit_balance(
    queryset: QuerySet,
    counterparty_field: str,
    direction,
    annotation_name: str = "credit_balance",
    currency_name: str = "credit_balance_currency",
) -> QuerySet:
    """
    Annotate a Client (or other counterparty) queryset with its credit: the
    sum of `Payment.amount_pending_allocation` over its confirmed payments in
    `direction` that still have money left to allocate. For a client and
    inbound payments that is the "saldo a favor" - what they have paid us
    that no invoice has consumed yet.

    `counterparty_field` is the FK on Payment pointing back at the queryset's
    model. The currency comes off one of the summed payments, EUR when there
    are none (the total is zero then anyway).
    """
    from payments.models import Payment

    payments = Payment.objects.filter(
        **{counterparty_field: OuterRef("pk")}
    ).filter(unallocated_payments_q(direction))

    total = (
        payments.values(counterparty_field)
        .annotate(total=Sum("amount_pending_allocation"))
        .values("total")
    )
    currency = payments.order_by("pk").values("amount_pending_allocation_currency")[:1]

    return queryset.annotate(**{
        annotation_name: Coalesce(
            Subquery(total, output_field=_DECIMAL_FIELD), _ZERO, output_field=_DECIMAL_FIELD
        ),
        currency_name: Coalesce(Subquery(currency), Value("EUR")),
    })


def annotate_amount_remaining(
    queryset: QuerySet, invoice_model, amount_field: str = "total_amount",
) -> QuerySet:
    """
    Annotate an invoice queryset (`Invoice`, `SalesInvoice`, …) with
    `db_amount_paid` and `db_amount_remaining`, computed at the database
    level via PaymentAllocation.

    Eliminates the N+1 caused by the model's Python `amount_paid` /
    `amount_remaining` properties when listing or aggregating invoices.
    `amount_field` names the document's total for Payables that don't call
    it `total_amount` (e.g. `LoanEvent.amount`).
    """
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

    return queryset.annotate(
        db_amount_paid=Coalesce(paid_subquery, _ZERO, output_field=_DECIMAL_FIELD),
        db_amount_remaining=Coalesce(
            F(amount_field) - Coalesce(paid_subquery, _ZERO, output_field=_DECIMAL_FIELD),
            _ZERO,
            output_field=_DECIMAL_FIELD,
        ),
    )
