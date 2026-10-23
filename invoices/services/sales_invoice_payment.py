from invoices import payment_reference as payment_references
from invoices.models import PaymentMethod


ALLOWED_SALES_INVOICE_PAYMENT_METHODS = {m.value for m in PaymentMethod}


def _display_for_method(method: str) -> str:
    try:
        return PaymentMethod(method).label
    except ValueError:
        return method


def effective_payment_method(*, sales_invoice, client=None) -> str:
    """
    How the invoice is paid: invoice snapshot first, then client preference,
    then transfer.
    """
    client = client or getattr(sales_invoice, "client", None)
    invoice_method = (getattr(sales_invoice, "payment_method", None) or "").strip()
    if invoice_method in ALLOWED_SALES_INVOICE_PAYMENT_METHODS:
        return invoice_method
    if client:
        client_method = (client.payment_method or "").strip()
        if client_method in ALLOWED_SALES_INVOICE_PAYMENT_METHODS:
            return client_method
    return PaymentMethod.TRANSFER


def effective_payment_method_display(*, sales_invoice, client=None) -> str:
    """
    Label for PDF/export: invoice snapshot first, then client preference.
    """
    return _display_for_method(effective_payment_method(sales_invoice=sales_invoice, client=client))


def transfer_payment_reference(sales_invoice) -> str:
    """
    The grouped payment reference for the client to put in the transfer
    concept, or "" when the invoice isn't paid by transfer (direct debit,
    cash…), where asking for it would only confuse.
    """
    if effective_payment_method(sales_invoice=sales_invoice) != PaymentMethod.TRANSFER:
        return ""
    return payment_references.display(sales_invoice.payment_reference)


def resolve_sales_invoice_payment_method(*, client, explicit: str | None = None) -> str:
    """
    Resolve payment method for a new sales invoice.

    Uses explicit value when valid; otherwise falls back to the client's preference.
    """
    method = (explicit or "").strip()
    if method in ALLOWED_SALES_INVOICE_PAYMENT_METHODS:
        return method
    if client and (client.payment_method or "").strip() in ALLOWED_SALES_INVOICE_PAYMENT_METHODS:
        return client.payment_method
    return PaymentMethod.TRANSFER
