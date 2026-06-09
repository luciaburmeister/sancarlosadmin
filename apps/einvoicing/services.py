"""
High-level orchestration service for e-invoicing.

This is the single entry point the rest of the codebase should use.
It resolves the correct account + provider and creates audit records.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.einvoicing.models import (
    EInvoicingAccount,
    EInvoicingProviderChoices,
    EInvoicingSubmission,
    SubmissionAction,
    SubmissionStatus,
)
from apps.einvoicing.providers.base import EInvoicingProvider
from apps.einvoicing.providers.verifacti import VerifactiProvider
from invoices.models import SalesInvoiceStatus
if TYPE_CHECKING:
    from invoices.models import SalesInvoice

logger = logging.getLogger(__name__)

# ── Provider factory ─────────────────────────────────────────────────────
_PROVIDER_MAP: dict[str, type[EInvoicingProvider]] = {
    EInvoicingProviderChoices.VERIFACTI: VerifactiProvider,
}


def get_provider(account: EInvoicingAccount) -> EInvoicingProvider:
    """Instantiate the correct provider for an account."""
    cls = _PROVIDER_MAP.get(account.provider)
    if cls is None:
        raise ValueError(f"Unknown e-invoicing provider: {account.provider}")
    return cls()


class EInvoicingService:
    """Orchestration layer — resolves account, calls provider, writes audit log."""

    # ── public API ───────────────────────────────────────────────────────

    @classmethod
    def submit_invoice(cls, sales_invoice: "SalesInvoice") -> EInvoicingSubmission | None:
        """
        Submit a finalized invoice to the e-invoicing provider.
        Returns the EInvoicingSubmission record, or None if no account is configured.
        """
        account = cls._resolve_account(sales_invoice)
        if account is None:
            logger.info(
                "No active e-invoicing account for company %s — skipping submission.",
                sales_invoice.company_id,
            )
            return None

        provider = get_provider(account)

        # Create a pending submission record
        submission = EInvoicingSubmission.objects.create(
            account=account,
            sales_invoice=sales_invoice,
            action=SubmissionAction.CREATE,
            status=SubmissionStatus.PENDING,
        )

        result = provider.submit_invoice(account, sales_invoice)

        submission.status = result.status
        submission.request_payload = result.request_payload
        submission.response_payload = result.response_payload
        submission.provider_record_id = result.provider_record_id
        submission.error_message = result.error_message
        submission.submitted_at = timezone.now()
        submission.save()

        if not result.success:
            logger.warning(
                "Invoice %s submission failed: %s",
                sales_invoice.pk,
                result.error_message,
            )

        return submission

    @classmethod
    def cancel_invoice(cls, sales_invoice: "SalesInvoice") -> EInvoicingSubmission | None:
        """
        Cancel a previously submitted invoice at the e-invoicing provider.
        Returns the EInvoicingSubmission record, or None if no account is configured.
        """
        account = cls._resolve_account(sales_invoice)
        if account is None:
            logger.info(
                "No active e-invoicing account for company %s — skipping cancellation.",
                sales_invoice.company_id,
            )
            return None

        provider = get_provider(account)

        submission = EInvoicingSubmission.objects.create(
            account=account,
            sales_invoice=sales_invoice,
            action=SubmissionAction.CANCEL,
            status=SubmissionStatus.PENDING,
        )

        result = provider.cancel_invoice(account, sales_invoice)

        submission.status = result.status
        submission.request_payload = result.request_payload
        submission.response_payload = result.response_payload
        submission.provider_record_id = result.provider_record_id
        submission.error_message = result.error_message
        submission.submitted_at = timezone.now()
        submission.save()

        if not result.success:
            logger.warning(
                "Invoice %s cancellation failed: %s",
                sales_invoice.pk,
                result.error_message,
            )
        else:
            sales_invoice.status = SalesInvoiceStatus.CANCELLED
            sales_invoice.save(update_fields=["status"])

        return submission

    @classmethod
    def check_account_health(cls, account: EInvoicingAccount) -> bool:
        """Test connectivity for an e-invoicing account."""
        provider = get_provider(account)
        return provider.check_health(account)

    # ── internal helpers ─────────────────────────────────────────────────

    @classmethod
    def _resolve_account(
        cls, sales_invoice: "SalesInvoice"
    ) -> EInvoicingAccount | None:
        """Find the active e-invoicing account for the invoice's company."""
        return (
            EInvoicingAccount.objects.filter(
                company=sales_invoice.company,
                is_active=True,
            )
            .order_by("pk")
            .first()
        )
