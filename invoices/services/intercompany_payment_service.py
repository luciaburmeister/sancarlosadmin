"""
Paying a purchase invoice from another group company, and taking it back.

See invoices.models.IntercompanyPayment for what one creates in each
company and how it is booked.
"""
import logging
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from djmoney.money import Money

from invoices import line_sources
from invoices import payment_reference as payment_references
from invoices.models import (
    ConfirmationStatus,
    IntercompanyPayment,
    IntercompanyPaymentStatus,
    Invoice,
    InvoiceDocumentType,
    InvoiceItem,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
)
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod, PaymentStatus

logger = logging.getLogger(__name__)

CURRENCY = "EUR"


class IntercompanyPaymentService:

    # -- who may ---------------------------------------------------------------

    @staticmethod
    def payer_choices(user, invoice):
        """
        Companies that can pay ``invoice`` for its company: the other ones the
        user can see (in a request, those active in the switcher), with the
        NIF the debit note is issued under.
        """
        from core.admin.company_access import get_user_companies

        return (
            get_user_companies(user)
            .exclude(pk=invoice.company_id)
            .exclude(tax_identifier__isnull=True)
            .exclude(tax_identifier="")
        )

    @staticmethod
    def can_pay(invoice) -> bool:
        """Whether ``invoice`` is one another company can pay: a provider's invoice still owed, in euros."""
        return bool(
            invoice.document_type == InvoiceDocumentType.INVOICE
            and invoice.provider_id
            and invoice.company_id
            and (invoice.currency or CURRENCY) == CURRENCY
            and invoice.amount_remaining.amount > 0
        )

    @classmethod
    def can_register(cls, user, invoice) -> bool:
        return (
            user.has_perm("invoices.can_register_intercompany_payment")
            and cls.can_pay(invoice)
            and cls.payer_choices(user, invoice).exists()
        )

    @staticmethod
    def can_see_both(user, record) -> bool:
        from core.admin.company_access import get_user_companies

        visible = set(get_user_companies(user).values_list("pk", flat=True))
        return {record.company_id, record.payer_company_id} <= visible

    # -- registering -------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def create(cls, *, user, source_invoice, payer_company, bank_account, payment_date, amount, reference=None):
        """
        ``payer_company`` paid ``amount`` of ``source_invoice`` from
        ``bank_account``. Creates and links, at once: the payer's payment and
        debit note, our settlement of the invoice and our debt to the payer.
        Raises PermissionDenied, or ValidationError naming what is wrong.
        """
        from core.services.client_service import get_or_create_client
        from core.services.provider_service import get_or_create_provider
        from invoices.services.sales_invoice_service import SalesInvoiceService

        source = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("company", "provider").get(pk=source_invoice.pk)
        )
        debtor = source.company
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))

        if not user.has_perm("invoices.can_register_intercompany_payment"):
            raise PermissionDenied
        # Only from a company the user can see, to one they can see too
        if not cls.payer_choices(user, source).filter(pk=payer_company.pk).exists():
            raise PermissionDenied
        cls._validate(source, payer_company, bank_account, amount)

        payer_client = get_or_create_client(
            debtor.tax_identifier, debtor.legal_name or debtor.name, payer_company,
            exclude_from_payment_reminders=True,
        )
        creditor_provider = get_or_create_provider(
            payer_company.tax_identifier, payer_company.legal_name or payer_company.name, debtor,
        )
        money = Money(amount, CURRENCY)
        number = source.invoice_number or str(source.pk)

        record = IntercompanyPayment.objects.create(
            company=debtor,
            source_invoice=source,
            payer_company=payer_company,
            payer_bank_account=bank_account,
            payer_client=payer_client,
            creditor_provider=creditor_provider,
            amount=money,
            payment_date=payment_date,
            created_by=user,
        )

        # The payer charges us what it paid: the debit note and our mirror of
        # it go by one reference, which the reimbursement's movement carries
        shared_reference = payment_references.assign_unique_for(debtor.pk, payer_company.pk)
        debit_note = SalesInvoice.objects.create(
            company=payer_company,
            client=payer_client,
            document_type=SalesInvoiceDocumentType.INTERCOMPANY,
            status=SalesInvoiceStatus.PROFORMA,
            date_issued=payment_date,
            payment_reference=shared_reference,
            exclude_from_payment_reminders=True,
            reference=number,
            currency=CURRENCY,
        )
        SalesInvoiceItem.objects.create(
            invoice=debit_note,
            description=(
                f"Pago por cuenta de {debtor.legal_name or debtor.name} a {source.provider.name}"
                f" - Factura {number}"
            ),
            quantity=1,
            unit_price=money,
            source_id=line_sources.source_id(line_sources.INTERCOMPANY, record.pk),
        )
        SalesInvoiceService.issue_intercompany_debit_note(debit_note, payment_date)

        # The payer's money out, booked against what we now owe it
        payer_payment = Payment.objects.create(
            company=payer_company,
            payment_date=payment_date,
            amount=money,
            direction=PaymentDirection.OUTBOUND,
            method=PaymentMethod.TRANSFER,
            status=PaymentStatus.CONFIRMED,
            bank_account=bank_account,
            client=payer_client,
            # What the payer writes in the transfer concept, so the movement is matched
            reference=reference or source.payment_reference or number,
            expected_bank_reference=source.payment_reference,
            notes=_("Paid for %(company)s: invoice %(number)s of %(provider)s")
            % {"company": debtor, "number": number, "provider": source.provider},
        )
        PaymentAllocation.objects.create(
            payment=payer_payment,
            content_type=ContentType.objects.get_for_model(IntercompanyPayment),
            object_id=record.pk,
            amount_applied=money,
        )

        # Ours: the invoice is paid, with no money of ours behind it
        settlement = Payment.objects.create(
            company=debtor,
            payment_date=payment_date,
            amount=money,
            direction=PaymentDirection.OUTBOUND,
            method=PaymentMethod.INTERCOMPANY,
            status=PaymentStatus.CONFIRMED,
            provider=source.provider,
            reference=debit_note.invoice_number,
            notes=_("Paid by %(company)s") % {"company": payer_company},
        )
        PaymentAllocation.objects.create(
            payment=settlement,
            content_type=ContentType.objects.get_for_model(Invoice),
            object_id=source.pk,
            amount_applied=money,
        )

        # And what we owe the payer instead, open in our payables
        mirror = Invoice.objects.create(
            company=debtor,
            provider=creditor_provider,
            document_type=InvoiceDocumentType.INTERCOMPANY,
            invoice_number=debit_note.invoice_number,
            date_issued=debit_note.date_issued,
            due_date=debit_note.due_date,
            net_amount=money,
            total_amount=money,
            currency=CURRENCY,
            payment_reference=shared_reference,
            skip_po_matching=True,
            ocr_processed=True,
            needs_manual_review=False,
            confirmation_status=ConfirmationStatus.APPROVED,
            image_hash="",
            observations=_("Debit note of %(company)s: it paid invoice %(number)s of %(provider)s for us.")
            % {"company": payer_company, "number": number, "provider": source.provider},
            uploaded_by=user,
        )
        InvoiceItem.objects.create(
            invoice=mirror,
            description=f"Nota de cargo {debit_note.invoice_number} - Factura {number}",
            quantity=1,
            unit_price=money,
        )

        # Payments are journalized once committed, with these links in place
        record.payer_payment = payer_payment
        record.settlement_payment = settlement
        record.debit_note = debit_note
        record.mirror_invoice = mirror
        record.save(update_fields=["payer_payment", "settlement_payment", "debit_note", "mirror_invoice"])

        transaction.on_commit(lambda record_id=record.pk: cls._queue_mirror_pdf(record_id))
        return record

    @staticmethod
    def _validate(source, payer_company, bank_account, amount):
        if source.document_type != InvoiceDocumentType.INVOICE:
            raise ValidationError(_("Only invoices can be paid by another company, not credit notes or debts."))
        if not source.provider_id:
            raise ValidationError(_("The invoice has no provider."))
        if (source.currency or CURRENCY) != CURRENCY:
            raise ValidationError(_("Only invoices in euros can be paid by another company."))
        if payer_company.pk == source.company_id:
            raise ValidationError(_("The paying company must be another one."))
        if not source.company.tax_identifier or not payer_company.tax_identifier:
            raise ValidationError(_("Both companies need a tax identifier: the debit note is issued under them."))
        if not bank_account or bank_account.company_id != payer_company.pk:
            raise ValidationError(_("The bank account must belong to the paying company."))
        remaining = source.amount_remaining.amount
        if amount <= 0:
            raise ValidationError(_("The amount must be greater than zero."))
        if amount > remaining:
            raise ValidationError(
                _("Cannot pay more than the %(remaining)s still outstanding.") % {"remaining": source.amount_remaining}
            )

    @staticmethod
    def _queue_mirror_pdf(record_id):
        try:
            from invoices.tasks import attach_intercompany_debit_note_pdf

            attach_intercompany_debit_note_pdf.delay(record_id)
        except Exception:
            logger.exception("Could not queue the debit note PDF of intercompany payment %s", record_id)

    # -- cancelling --------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def cancel(cls, record, user):
        """
        Undo everything ``record`` created: the invoice is owed again, both
        payments are cancelled (their entries reversed), the debit note is
        cancelled and our debt to the payer deleted. Refused once the payer's
        bank movement is reconciled or the debt is being repaid.
        """
        from banking.models import BankReconciliation

        record = IntercompanyPayment.objects.select_for_update().get(pk=record.pk)
        if not user.has_perm("invoices.can_cancel_intercompany_payment") or not cls.can_see_both(user, record):
            raise PermissionDenied
        if record.status != IntercompanyPaymentStatus.ACTIVE:
            raise ValidationError(_("This intercompany payment is already cancelled."))
        if BankReconciliation.objects.filter(payment_id=record.payer_payment_id).exists():
            raise ValidationError(
                _("The payer's bank movement is already reconciled with it: undo that reconciliation first.")
            )
        for document in (record.debit_note, record.mirror_invoice):
            if document and PaymentAllocation.objects.filter(
                content_type=ContentType.objects.get_for_model(document), object_id=document.pk,
            ).exists():
                raise ValidationError(
                    _("%(document)s has payments: the debt is being repaid, it can no longer be cancelled.")
                    % {"document": document.invoice_number}
                )

        for payment in (record.payer_payment, record.settlement_payment):
            if payment:
                for allocation in payment.allocations.all():
                    allocation.delete()
                payment.status = PaymentStatus.CANCELLED
                payment.save(update_fields=["status"])

        if record.debit_note:
            record.debit_note.status = SalesInvoiceStatus.CANCELLED
            record.debit_note.save(update_fields=["status"])

        mirror = record.mirror_invoice
        record.mirror_invoice = None
        record.status = IntercompanyPaymentStatus.CANCELLED
        record.cancelled_by = user
        record.cancelled_at = timezone.now()
        record.save(update_fields=["mirror_invoice", "status", "cancelled_by", "cancelled_at"])
        if mirror:
            mirror.delete()
        return record
