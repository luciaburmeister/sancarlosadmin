"""Issuing a promissory note: it settles its document(s) immediately, the bank charge comes later."""
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils.translation import gettext as _

from payments.models import (
    Payment, PaymentAllocation, PaymentDirection, PaymentMethod, PaymentStatus,
    PromissoryNote, PromissoryNoteStatus,
)


class PromissoryNoteService:
    """
    Issues a promissory note: creates it together with the Payment and
    PaymentAllocation that settle `document` for `applied_amount`, so the
    note clears the invoice the same day it's handed over or received. The
    bank charge at due_date is a separate, later event - see the
    reconciliation matcher, which settles the note itself, never the
    invoice again.

    `money` is the note's full face value; `applied_amount` is however much
    of it settles `document` (may be less, when the rest is distributed to
    other invoices of the same counterparty via the existing surplus-
    distribution step, reusing this same Payment).
    """

    @staticmethod
    def issue(
        *, document, money, applied_amount, number, issue_date, due_date,
        bank_account=None, note_document=None, reference=None,
    ):
        provider = getattr(document, "provider", None)
        client = getattr(document, "client", None)
        direction = PaymentDirection.OUTBOUND if provider else PaymentDirection.INBOUND

        with transaction.atomic():
            payment = Payment.objects.create(
                company=document.company,
                payment_date=issue_date,
                amount=money,
                direction=direction,
                method=PaymentMethod.PROMISSORY_NOTE,
                status=PaymentStatus.CONFIRMED,
                reference=reference,
                amount_pending_allocation=money,
            )
            PaymentAllocation.objects.create(
                payment=payment,
                content_type=ContentType.objects.get_for_model(document),
                object_id=document.pk,
                amount_applied=applied_amount,
            )
            return PromissoryNote.objects.create(
                company=document.company,
                provider=provider,
                client=client,
                number=number,
                amount=money,
                issue_date=issue_date,
                due_date=due_date,
                bank_account=bank_account,
                document=note_document,
                payment=payment,
            )

    @staticmethod
    def cancel(note):
        """
        Cancel a note still issued, before its due date was reconciled: its
        invoice allocation is removed (the invoice owes again) and its
        issuance Payment is cancelled, reversing its journal entry as of
        today - not the original issue_date, so a closed accounting period
        is never touched.
        """
        if note.status != PromissoryNoteStatus.ISSUED:
            raise ValueError(
                _("Only a note still issued, not yet matured, can be cancelled.")
            )
        with transaction.atomic():
            note.payment.allocations.exclude(is_mirror=True).delete()
            note.payment.status = PaymentStatus.CANCELLED
            note.payment.save(update_fields=["status"])
            note.status = PromissoryNoteStatus.CANCELLED
            note.save(update_fields=["status"])
        return note

    @staticmethod
    def mark_returned(note):
        """
        A note still in portfolio bounces at the bank: its sales invoice
        allocation is removed (the invoice is owed again) and its issuance
        Payment is cancelled, reversing its journal entry as of today.
        """
        if note.status != PromissoryNoteStatus.IN_PORTFOLIO:
            raise ValueError(
                _("Only a note still in portfolio, not yet collected, can be marked as returned.")
            )
        with transaction.atomic():
            note.payment.allocations.exclude(is_mirror=True).delete()
            note.payment.status = PaymentStatus.CANCELLED
            note.payment.save(update_fields=["status"])
            note.status = PromissoryNoteStatus.RETURNED
            note.save(update_fields=["status"])
        return note
