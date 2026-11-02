"""Credit notes (facturas rectificativas) reversing a whole issued invoice.

A credit note is the exact negative mirror of the invoice it reverses: every
line (same source, expediente and business unit), the header discount and the
taxes, advance deductions included. Nothing is recomputed, so taxes and
discounts come out exactly as the invoice had them. Because the lines keep
their sources, everything that sums what an expediente has billed nets the two
out, and the reversed items can be billed again; what is linked to an invoice
rather than summed (delivery orders, configurations, work entries) follows
SalesInvoiceQuerySet.live, which leaves a reversed invoice out.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import formats, timezone
from django.utils.translation import gettext as _
from djmoney.money import Money

from invoices.models import (
    AppliedTax,
    RectificationCode,
    RectificationReason,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
)
from invoices.services.sales_invoice_service import SalesInvoiceService


class CreditNoteService:

    @classmethod
    def validate_reversible(cls, invoice):
        """
        Raises ValidationError unless ``invoice`` can be reversed: an issued
        regular invoice, not reversed already and not being cancelled at the
        AEAT. Advances and debit notes are refused.
        """
        from apps.einvoicing.models import SubmissionAction, SubmissionStatus

        if invoice.document_type != SalesInvoiceDocumentType.INVOICE:
            raise ValidationError(
                _("Only invoices can be reversed; %(number)s is a %(type)s.")
                % {"number": invoice.display_number, "type": invoice.get_document_type_display()}
            )
        if invoice.status != SalesInvoiceStatus.FINAL:
            raise ValidationError(
                _("Only issued invoices can be reversed; %(number)s is %(status)s.")
                % {"number": invoice.display_number, "status": invoice.get_status_display()}
            )
        rectification = invoice.live_rectification
        if rectification:
            raise ValidationError(
                _("%(number)s is already reversed by credit note %(credit_note)s.")
                % {"number": invoice.display_number, "credit_note": rectification.display_number}
            )
        if invoice.einvoicing_submissions.filter(
            action=SubmissionAction.CANCEL,
            status__in=[SubmissionStatus.PENDING, SubmissionStatus.SUBMITTED, SubmissionStatus.ACCEPTED],
        ).exists():
            raise ValidationError(
                _("%(number)s is being cancelled at the AEAT: it cannot be reversed.")
                % {"number": invoice.display_number}
            )

    @classmethod
    def can_reverse(cls, invoice):
        try:
            cls.validate_reversible(invoice)
        except ValidationError:
            return False
        return True

    @staticmethod
    def notes_for(original, reason, detail=""):
        """What the credit note says about itself: the invoice it reverses and why."""
        notes = _("Credit note rectifying %(number)s (%(date)s). Reason: %(reason)s.") % {
            "number": original.invoice_number,
            "date": formats.date_format(original.date_issued, "d/m/Y"),
            "reason": RectificationReason(reason).label,
        }
        detail = (detail or "").strip()
        return f"{notes} {detail}" if detail else notes

    @classmethod
    @transaction.atomic
    def reverse(cls, invoice, *, code=RectificationCode.R1, reason, detail="", send_email=True,
                reply_to=None, cc_emails=None):
        """
        Issue the credit note reversing the whole of ``invoice`` and return it.

        The credit note is issued at once (numbered in the credit note
        series, journalized, sent to the AEAT and the client). What the
        invoice still had to collect is offset against it, so both are
        settled; what the client already paid stays open on the credit note,
        owed to them, to refund or offset against another invoice.

        Raises:
            ValidationError when the invoice cannot be reversed, or the
            reason is "other" with no detail.
        """
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        if reason not in RectificationReason.values:
            raise ValidationError(_("Choose why the invoice is being rectified."))
        if code not in RectificationCode.values:
            raise ValidationError(_("Choose the rectification type."))
        detail = (detail or "").strip()
        if reason == RectificationReason.OTHER and not detail:
            raise ValidationError(_("Explain why the invoice is being rectified."))

        # Expedientes first, as every invoicing path takes them, then the invoice
        project_file_ids = ProjectFileInvoiceService.project_file_ids_of(invoice)
        ProjectFileInvoiceService.lock_project_files(project_file_ids)
        invoice = SalesInvoice.objects.select_for_update(of=("self",)).get(pk=invoice.pk)
        cls.validate_reversible(invoice)

        credit_note = cls._mirror(invoice, code=code, reason=reason, detail=detail)
        SalesInvoiceService.finalize_invoice(
            credit_note, send_email=send_email, reply_to=reply_to, cc_emails=cc_emails,
        )

        ProjectFileInvoiceService.unlock_work_entries(
            invoice.items.values_list("source_id", flat=True)
        )

        outstanding = invoice.amount_remaining_decimal
        if outstanding > 0:
            cls.offset(invoice, credit_note, outstanding)

        # The post_save signal covers the header and bulk expedientes; a
        # line's own expediente is recalculated here
        from management.models import ProjectFile

        for project_file in ProjectFile.objects.filter(pk__in=project_file_ids):
            project_file.recalculate_total_amount()
        return credit_note

    @classmethod
    def _mirror(cls, invoice, *, code, reason, detail):
        """The pro forma credit note: ``invoice`` with every amount negated."""
        currency = invoice.currency
        credit_note = SalesInvoice.objects.create(
            company=invoice.company,
            document_type=SalesInvoiceDocumentType.CREDIT_NOTE,
            status=SalesInvoiceStatus.PROFORMA,
            rectifies=invoice,
            rectification_code=code,
            rectification_reason=reason,
            rectification_detail=detail,
            client=invoice.client,
            project_file=invoice.project_file,
            currency=currency,
            date_issued=timezone.localdate(),
            reference=invoice.reference,
            payment_method=invoice.payment_method,
            payment_terms_days=invoice.payment_terms_days,
            discount_amount=Money(-invoice.discount_amount.amount, currency),
            discount_breakdown=[cls._negated_entry(entry) for entry in invoice.discount_breakdown or []],
            notes=cls.notes_for(invoice, reason, detail),
        )
        credit_note.project_files.set(invoice.project_files.all())

        for item in invoice.items.order_by("pk").prefetch_related("applied_taxes"):
            line = SalesInvoiceItem(
                invoice=credit_note,
                description=item.description,
                quantity=-item.quantity,
                discount_percentage=item.discount_percentage,
                unit_price=item.unit_price,
                source_id=item.source_id,
                quote_name=item.quote_name,
                reference=item.reference,
                project_file_id=item.project_file_id,
                advance_line_id=item.advance_line_id,
                business_unit_id=item.business_unit_id,
            )
            # Pinned to the exact opposite, whatever rounding the % would give
            line.pin_total(Money(-item.line_total.amount, currency))
            line.save()
            cls._copy_negated_taxes(item.applied_taxes.all(), line)

        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        cls._copy_negated_taxes(
            AppliedTax.objects.filter(content_type=invoice_ct, object_id=invoice.pk).order_by("order", "pk"),
            credit_note,
        )

        # What was issued, negated: not recomputed, so an invoice whose
        # stored totals drifted from its lines is still reversed exactly
        for field in ("subtotal_amount", "tax_amount", "total_amount"):
            setattr(credit_note, field, Money(-getattr(invoice, field).amount, currency))
        credit_note.save(update_fields=["subtotal_amount", "tax_amount", "total_amount"])
        return credit_note

    @staticmethod
    def _copy_negated_taxes(taxes, target):
        target_ct = ContentType.objects.get_for_model(target)
        for tax in taxes:
            AppliedTax.objects.create(
                content_type=target_ct,
                object_id=target.pk,
                tax_type=tax.tax_type,
                selected_percentage=tax.selected_percentage,
                base_amount=None if tax.base_amount is None else Money(-tax.base_amount.amount, tax.base_amount.currency),
                calculated_amount=Money(-tax.calculated_amount.amount, tax.calculated_amount.currency),
                order=tax.order,
            )

    @staticmethod
    def _negated_entry(entry):
        negated = dict(entry)
        for key in ("amount", "base"):
            if negated.get(key) is not None:
                negated[key] = str(-Decimal(str(negated[key])))
        return negated

    @classmethod
    @transaction.atomic
    def offset(cls, invoice, credit_note, amount):
        """
        Settle ``amount`` of ``invoice`` with ``credit_note``, the client's
        own: a compensation that moves no money, so it books nothing (the
        two invoices' entries already net out on the client's account).

        Raises:
            ValidationError when they are not the same client's, issued,
            or ``amount`` is more than either has open.
        """
        from payments.models import (
            Payment,
            PaymentAllocation,
            PaymentCreatedBy,
            PaymentDirection,
            PaymentMethod,
            PaymentStatus,
        )

        amount = Decimal(str(amount))
        invoice = SalesInvoice.objects.select_for_update().get(pk=invoice.pk)
        credit_note = SalesInvoice.objects.select_for_update().get(pk=credit_note.pk)
        if not credit_note.is_credit_note or invoice.is_credit_note:
            raise ValidationError(_("A credit note is offset against an invoice."))
        if invoice.status != SalesInvoiceStatus.FINAL or credit_note.status != SalesInvoiceStatus.FINAL:
            raise ValidationError(_("Only issued invoices and credit notes can be offset."))
        if (invoice.company_id, invoice.client_id, invoice.currency) != (
            credit_note.company_id, credit_note.client_id, credit_note.currency,
        ):
            raise ValidationError(_("The credit note and the invoice must be the same client's, in the same currency."))
        if amount <= 0 or amount > invoice.amount_remaining_decimal or amount > -credit_note.amount_remaining_decimal:
            raise ValidationError(
                _("%(amount)s is more than %(invoice)s or %(credit_note)s have open.")
                % {"amount": amount, "invoice": invoice.display_number, "credit_note": credit_note.display_number}
            )

        payment = Payment.objects.create(
            company=invoice.company,
            payment_date=timezone.localdate(),
            amount=Money(0, invoice.currency),
            direction=PaymentDirection.INBOUND,
            method=PaymentMethod.COMPENSATION,
            status=PaymentStatus.CONFIRMED,
            created_by=PaymentCreatedBy.AUTO,
            client=invoice.client,
            reference=credit_note.invoice_number,
            notes=_("%(credit_note)s offset against %(invoice)s") % {
                "credit_note": credit_note.invoice_number, "invoice": invoice.invoice_number,
            },
        )
        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        for document, applied in ((invoice, amount), (credit_note, -amount)):
            PaymentAllocation.objects.create(
                payment=payment,
                content_type=invoice_ct,
                object_id=document.pk,
                amount_applied=Money(applied, invoice.currency),
            )
            document.recalculate_payment_status()
        payment.recalculate_pending_allocation()
        return payment
