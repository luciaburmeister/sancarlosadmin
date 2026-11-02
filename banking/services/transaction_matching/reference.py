"""Find the invoices a bank transaction names by payment reference.

A payer who writes an invoice's payment reference in the transfer concept
tells us exactly which invoice the money is for: inbound movements look in
SalesInvoice, outbound ones in purchase Invoice. See invoices/payment_reference.py
for the format.

A payment registered before its movement reached the bank (another group
company paying one of ours, see invoices.models.IntercompanyPayment) says
which reference that movement will carry: the movement is that payment.
"""
from django.apps import apps

from invoices import payment_reference as payment_references
from payments.mixins import annotate_amount_remaining
from payments.models import PaymentDirection

UNPAID_STATUSES = ["unpaid", "partial"]
# Shorter invoice numbers ("12") turn up in any concept by chance
MIN_NAMED_REFERENCE_LENGTH = 5


def find_referenced_documents(transaction, direction) -> list:
    """Unpaid invoices of the transaction's company whose reference is in its concept."""
    refs = payment_references.find_in_text(transaction.description)
    if not refs:
        return []

    if direction == PaymentDirection.INBOUND:
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")
        # Credit notes are paid by us, never to us
        qs = SalesInvoice.objects.filter(
            company=transaction.company,
            payment_reference__in=refs,
            payment_status__in=UNPAID_STATUSES,
        ).exclude(status="cancelled").exclude(document_type="credit_note").select_related("client")
        model = SalesInvoice
    else:
        Invoice = apps.get_model("invoices", "Invoice")
        # Credit notes are paid to us, never by us
        qs = Invoice.objects.filter(
            company=transaction.company,
            payment_reference__in=refs,
            payment_status__in=UNPAID_STATUSES,
        ).exclude(document_type="credit_note").select_related("provider")
        model = Invoice

    docs = list(annotate_amount_remaining(qs, model))
    # Keep the order the payer wrote them in
    docs.sort(key=lambda doc: refs.index(doc.payment_reference))
    return docs


def find_expected_payments(transaction, direction) -> list:
    """
    Booked payments of the transaction's company and bank account, not yet
    linked to any movement, that expect a reference in its concept.
    """
    refs = payment_references.find_in_text(transaction.description)
    if not refs:
        return []
    Payment = apps.get_model("payments", "Payment")
    return list(
        Payment.objects.filter(
            company=transaction.company,
            bank_account=transaction.bank_account,
            direction=direction,
            status__in=["confirmed", "reconciled"],
            expected_bank_reference__in=refs,
            reconciliation__isnull=True,
        ).order_by("payment_date", "pk")
    )


def find_payments_named_in_concept(transaction, direction) -> list:
    """
    Payments expecting a movement (see find_expected_payments) for exactly
    its amount whose own reference, the paid invoice's number, is in its
    concept: a payer who typed the invoice number instead of the reference.
    Only ever a suggestion.
    """
    concept = payment_references.compact(transaction.description)
    if not concept:
        return []
    Payment = apps.get_model("payments", "Payment")
    candidates = Payment.objects.filter(
        company=transaction.company,
        bank_account=transaction.bank_account,
        direction=direction,
        status__in=["confirmed", "reconciled"],
        expected_bank_reference__isnull=False,
        reconciliation__isnull=True,
        amount=abs(transaction.amount.amount),
    ).exclude(reference__isnull=True).order_by("payment_date", "pk")
    return [
        payment for payment in candidates
        if len(payment_references.compact(payment.reference)) >= MIN_NAMED_REFERENCE_LENGTH
        and payment_references.compact(payment.reference) in concept
    ]


def counterparty_of(documents: list):
    """The client/provider all ``documents`` share, or None."""
    parties = {doc.client if hasattr(doc, "client") else doc.provider for doc in documents}
    if len(parties) == 1:
        return parties.pop()
    return None
