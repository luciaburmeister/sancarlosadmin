"""Mixin for any document that can receive payments (Invoice, SalesInvoice, etc.)."""
from decimal import Decimal

from django.db import models


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
