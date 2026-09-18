from invoices.models import PaymentMethod


ALLOWED_PURCHASE_INVOICE_PAYMENT_METHODS = {m.value for m in PaymentMethod}


def _valid(method) -> str | None:
    method = (method or "").strip()
    return method if method in ALLOWED_PURCHASE_INVOICE_PAYMENT_METHODS else None


def resolve_purchase_invoice_payment_method(
    *, provider, extracted: str | None = None, current: str | None = None
) -> str:
    """
    Resolve the payment method for an OCR-processed purchase invoice.

    The provider's configured payment method is the source of truth: it is
    what accounting agreed with the vendor (SEPA, confirming, ...) and the OCR
    can only ever guess between transfer/cash/card from the document text.
    Only when no provider is linked do we fall back to the OCR reading, then
    to whatever the invoice already had, then to transfer.
    """
    if provider is not None:
        provider_method = _valid(getattr(provider, "payment_method", None))
        if provider_method:
            return provider_method
    return _valid(extracted) or _valid(current) or PaymentMethod.TRANSFER
