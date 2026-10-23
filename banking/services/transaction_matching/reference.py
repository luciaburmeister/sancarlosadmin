"""Find the invoices a bank transaction names by payment reference.

A payer who writes an invoice's payment reference in the transfer concept
tells us exactly which invoice the money is for: inbound movements look in
SalesInvoice, outbound ones in purchase Invoice. See invoices/payment_reference.py
for the format.
"""
from django.apps import apps

from invoices import payment_reference as payment_references
from payments.mixins import annotate_amount_remaining
from payments.models import PaymentDirection

UNPAID_STATUSES = ["unpaid", "partial"]


def find_referenced_documents(transaction, direction) -> list:
    """Unpaid invoices of the transaction's company whose reference is in its concept."""
    refs = payment_references.find_in_text(transaction.description)
    if not refs:
        return []

    if direction == PaymentDirection.INBOUND:
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")
        qs = SalesInvoice.objects.filter(
            company=transaction.company,
            payment_reference__in=refs,
            payment_status__in=UNPAID_STATUSES,
        ).exclude(status="cancelled").select_related("client")
        model = SalesInvoice
    else:
        Invoice = apps.get_model("invoices", "Invoice")
        # Credit notes are paid to us, never by us
        qs = Invoice.objects.filter(
            company=transaction.company,
            payment_reference__in=refs,
            payment_status__in=UNPAID_STATUSES,
            document_type="invoice",
        ).select_related("provider")
        model = Invoice

    docs = list(annotate_amount_remaining(qs, model))
    # Keep the order the payer wrote them in
    docs.sort(key=lambda doc: refs.index(doc.payment_reference))
    return docs


def counterparty_of(documents: list):
    """The client/provider all ``documents`` share, or None."""
    parties = {doc.client if hasattr(doc, "client") else doc.provider for doc in documents}
    if len(parties) == 1:
        return parties.pop()
    return None
