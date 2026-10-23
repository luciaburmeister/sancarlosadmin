"""
High-level orchestration service for e-invoicing.

This is the single entry point the rest of the codebase should use.
It resolves the correct account + provider and creates audit records.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

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

        Under the invoice's row lock it is checked again
        (SalesInvoiceService.validate_for_cancel: an advance still deducted
        or paid is refused), and a PENDING cancel submission is recorded in
        the same transaction: from then on an advance can no longer be
        deducted. The provider is called once that is committed.

        Without an active e-invoicing account an invoice that was never sent
        to the AEAT is cancelled locally; one that was sent is refused.

        Returns the EInvoicingSubmission record, or None if no account is
        configured (cancelled locally) or it was cancelled already.
        Raises ValidationError if it cannot be cancelled.
        """
        from invoices.services.sales_invoice_service import SalesInvoiceService

        account = cls._resolve_account(sales_invoice)
        with transaction.atomic():
            SalesInvoiceService.lock_for_cancel(sales_invoice)
            if sales_invoice.status == SalesInvoiceStatus.CANCELLED:
                return None
            SalesInvoiceService.validate_for_cancel(sales_invoice)
            if account is None:
                # An account switched off after the invoice was registered:
                # cancelling it here would leave the AEAT's record in force
                if EInvoicingSubmission.objects.filter(
                    sales_invoice=sales_invoice,
                    action=SubmissionAction.CREATE,
                    status__in=(
                        SubmissionStatus.PENDING,
                        SubmissionStatus.SUBMITTED,
                        SubmissionStatus.ACCEPTED,
                    ),
                ).exists():
                    raise ValidationError(
                        _("Invoice %(invoice)s is registered at the AEAT: reactivate the "
                          "company's e-invoicing account to cancel it.")
                        % {"invoice": sales_invoice.display_number}
                    )
                sales_invoice.status = SalesInvoiceStatus.CANCELLED
                sales_invoice.save(update_fields=["status"])
                logger.info(
                    "No active e-invoicing account for company %s — SalesInvoice %s "
                    "cancelled locally.",
                    sales_invoice.company_id,
                    sales_invoice.pk,
                )
                return None
            submission = EInvoicingSubmission.objects.create(
                account=account,
                sales_invoice=sales_invoice,
                action=SubmissionAction.CANCEL,
                status=SubmissionStatus.PENDING,
            )

        try:
            provider = get_provider(account)
            result = provider.cancel_invoice(account, sales_invoice)
        except Exception as exc:
            # The provider reports its own request failures: anything raised
            # here happened before the request was sent (e.g. an undecryptable
            # API key), so the marker must not keep an advance out of reach.
            submission.status = SubmissionStatus.ERROR
            submission.error_message = str(exc) or type(exc).__name__
            submission.submitted_at = timezone.now()
            submission.save(update_fields=["status", "error_message", "submitted_at"])
            raise

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
        """
        Find the active e-invoicing account for the invoice's company. None
        for a debit note, which is no invoice and never goes to the AEAT.
        """
        if sales_invoice.is_intercompany:
            return None
        return (
            EInvoicingAccount.objects.filter(
                company=sales_invoice.company,
                is_active=True,
            )
            .order_by("pk")
            .first()
        )
