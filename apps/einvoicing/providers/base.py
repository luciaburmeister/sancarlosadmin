"""
Abstract base class for e-invoicing providers.

Any provider (Verifacti, future alternatives) must implement this interface.
Swapping providers = writing a new subclass + registering it in the factory.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.einvoicing.models import EInvoicingAccount
    from invoices.models import SalesInvoice


@dataclass
class SubmissionResult:
    """Uniform result object returned by every provider method."""

    success: bool
    status: str  # 'accepted', 'rejected', 'error', 'submitted'
    provider_record_id: str = ""
    request_payload: dict = field(default_factory=dict)
    response_payload: dict = field(default_factory=dict)
    error_message: str = ""


class EInvoicingProvider(ABC):
    """Contract that every e-invoicing provider must fulfil."""

    @abstractmethod
    def submit_invoice(
        self, account: "EInvoicingAccount", invoice: "SalesInvoice"
    ) -> SubmissionResult:
        """Register a new invoice with the tax authority."""
        ...

    @abstractmethod
    def cancel_invoice(
        self, account: "EInvoicingAccount", invoice: "SalesInvoice"
    ) -> SubmissionResult:
        """Cancel / annul a previously registered invoice."""
        ...

    @abstractmethod
    def get_invoice_status(
        self,
        account: "EInvoicingAccount",
        serie: str,
        numero: str,
        fecha_expedicion: str,
    ) -> dict:
        """Check the registration status of a specific invoice."""
        ...

    @abstractmethod
    def check_health(self, account: "EInvoicingAccount") -> bool:
        """Return True if the provider API is reachable and the key is valid."""
        ...
