"""
Paying purchase invoices from our bank through Arcopay PSD2 payment
initiation, and following each order until the bank settles it.

Only from a bank account whose PSD2 consent is valid (it is re-checked before
sending), only to the provider's IBAN, and always with the invoice's payment
reference at the start of the concept: the bank movement then names the
invoice the matcher reconciles it to (banking/services/transaction_matching/
reference.py), and the provider sees its own invoice number next to it.

A single invoice goes as /directPayment/; several as one /bulkPayment/, signed
once. The user signs at the bank (``follow_url``); when Arcopay reports the
order done (OK), one confirmed Payment per invoice is registered expecting its
reference, so the invoice reads paid at once and the synced movement links
to that payment instead of creating another.
"""
import logging
import re
import unicodedata
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from djmoney.money import Money

from banking.models import BankPaymentOrder, BankPaymentOrderKind, BankPaymentOrderLine, BankPaymentOrderStatus
from banking.services.arcopay_import import ArcopayAPIError, ArcopayService
from company.models import BankAccount, ConsentStatus
from invoices.models import Invoice, InvoiceDocumentType, PaymentMethod as InvoicePaymentMethod
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod, PaymentStatus

logger = logging.getLogger(__name__)

CURRENCY = "EUR"
CONCEPT_MAX_LENGTH = 140
# Unsigned orders are given up on after this long
PENDING_EXPIRY = timedelta(days=7)

# /payment/status/ ``status``: anything else leaves the order pending
COMPLETED_BANK_STATUSES = {"OK"}
REJECTED_BANK_STATUSES = {"KO", "RJCT"}
CANCELLED_BANK_STATUSES = {"CNCL", "CANC"}

# Characters every SEPA bank accepts in a concept
_SEPA_UNSAFE = re.compile(r"[^A-Za-z0-9/\-?:().,'+ ]")


def _sepa_text(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", _SEPA_UNSAFE.sub(" ", ascii_text)).strip()


class BankPaymentService:

    # -- who may, from where, to whom ---------------------------------------------

    @staticmethod
    def source_accounts(company):
        """Accounts of ``company`` a payment can leave from: connected, with a valid consent."""
        return (
            BankAccount.objects.filter(company=company, consent_status=ConsentStatus.VALID)
            .exclude(nuek_token="")
            .exclude(iban="")
            .order_by("-is_default", "bank_name", "pk")
        )

    @classmethod
    def can_pay_from_bank(cls, user, company) -> bool:
        from core.admin.company_access import get_user_companies

        return bool(
            company
            and user.has_perm("banking.can_initiate_bank_payment")
            and get_user_companies(user).filter(pk=company.pk).exists()
            and cls.source_accounts(company).exists()
        )

    @staticmethod
    def destination_account(invoice):
        """The provider's account to pay into: its default one with an IBAN, or its only one."""
        if not invoice.provider_id:
            return None
        accounts = list(invoice.provider.bank_accounts.exclude(iban="").order_by("-is_default", "pk"))
        if accounts and (accounts[0].is_default or len(accounts) == 1):
            return accounts[0]
        return None

    @staticmethod
    def concept(invoice) -> str:
        """The invoice's reference first, so a bank that cuts the concept never cuts it."""
        number = _sepa_text(invoice.invoice_number or "")
        concept = f"{invoice.payment_reference} Factura {number}" if number else invoice.payment_reference
        return concept[:CONCEPT_MAX_LENGTH]

    @staticmethod
    def invoices_in_pending_orders(invoice_ids) -> set:
        return set(
            BankPaymentOrderLine.objects.filter(
                invoice_id__in=invoice_ids, order__status=BankPaymentOrderStatus.PENDING,
            ).values_list("invoice_id", flat=True)
        )

    @classmethod
    def ineligibility_reason(cls, invoice, pending_ids=frozenset()) -> str | None:
        """Why ``invoice`` cannot be paid from the bank, or None."""
        if invoice.document_type not in (InvoiceDocumentType.INVOICE, InvoiceDocumentType.INTERCOMPANY):
            return _("Credit notes are not paid by us.")
        if not invoice.provider_id:
            return _("The invoice has no provider.")
        if invoice.payment_method != InvoicePaymentMethod.TRANSFER:
            return _("It is paid by %(method)s, not by transfer.") % {"method": invoice.get_payment_method_display()}
        if (invoice.currency or CURRENCY) != CURRENCY:
            return _("Only invoices in euros can be paid from the bank.")
        if not invoice.payment_reference:
            return _("The invoice has no payment reference.")
        if invoice.amount_remaining.amount <= 0:
            return _("Nothing is left to pay.")
        if invoice.pk in pending_ids:
            return _("A bank payment for it is already awaiting signature.")
        if not cls.destination_account(invoice):
            return _("The provider has no default bank account with an IBAN.")
        return None

    @classmethod
    def split_eligible(cls, invoices):
        """``(payable, [(invoice, reason), ...])`` for the given invoices."""
        invoices = list(invoices)
        pending_ids = cls.invoices_in_pending_orders([invoice.pk for invoice in invoices])
        payable, refused = [], []
        for invoice in invoices:
            reason = cls.ineligibility_reason(invoice, pending_ids)
            if reason:
                refused.append((invoice, reason))
            else:
                payable.append(invoice)
        return payable, refused

    # -- sending ------------------------------------------------------------------

    @staticmethod
    def resolve_service(bank_account) -> str:
        """
        The Arcopay bank portal the account's consent was given at. Accounts
        connected before it was recorded are looked up by IBAN, which only
        works when the IBAN maps to one portal (banks with a separate business
        portal map to several: reconnect those).
        """
        if bank_account.nuek_service:
            return bank_account.nuek_service
        services = ArcopayService.iban_to_service(bank_account.iban)
        if len(services) != 1:
            raise ValidationError(
                _("Cannot tell which online banking %(bank)s uses. Reconnect the bank account once to send payments from it.")
                % {"bank": bank_account.bank_name or bank_account.iban}
            )
        bank_account.nuek_service = services[0]
        bank_account.save(update_fields=["nuek_service"])
        return bank_account.nuek_service

    @classmethod
    def initiate(cls, *, user, bank_account, amounts, return_url_for):
        """
        Send a transfer of ``amounts[invoice]`` to each invoice's provider from
        ``bank_account`` and return the order, whose ``follow_url`` is where the
        user signs it. ``return_url_for(order)`` is where the bank sends the
        user back. Raises PermissionDenied, or ValidationError naming what is
        wrong; an order Arcopay refused is kept with status ERROR.
        """
        if not amounts:
            raise ValidationError(_("Select at least one invoice to pay."))
        if not cls.can_pay_from_bank(user, bank_account.company):
            raise PermissionDenied
        if not cls.source_accounts(bank_account.company).filter(pk=bank_account.pk).exists():
            raise ValidationError(_("Payments can only leave from a connected account with a valid consent."))
        # The stored status may be stale: ask the bank before moving money
        try:
            consent = ArcopayService.check_consent_status(bank_account)
        except Exception as e:
            raise ValidationError(_("Could not check the bank consent: %(error)s") % {"error": e})
        if consent != ConsentStatus.VALID:
            raise ValidationError(
                _("The bank consent of %(bank)s is no longer valid. Renew or reconnect it first.")
                % {"bank": bank_account.bank_name or bank_account.iban}
            )
        service = cls.resolve_service(bank_account)

        order = cls._create_order(user, bank_account, amounts)
        lines = list(order.lines.all())
        backend_url = settings.BACKEND_URL.rstrip("/")
        urls = {
            "callback_url": f"{backend_url}/api/banking/arcopay/payment-callback/?order={order.token}",
            "callback_error_url": f"{backend_url}/api/banking/arcopay/payment-callback-error/?order={order.token}",
            "redirect_url": return_url_for(order),
        }
        owner_name = bank_account.holder_name or bank_account.company.legal_name or bank_account.company.name
        try:
            if len(lines) == 1:
                line = lines[0]
                result = ArcopayService.initiate_direct_payment(
                    bank_service=service,
                    owner_name=owner_name,
                    source_iban=order.source_iban,
                    destination_iban=line.destination_iban,
                    creditor_name=line.creditor_name,
                    amount=line.amount.amount,
                    currency=CURRENCY,
                    description=line.concept,
                    **urls,
                )
            else:
                result = ArcopayService.initiate_bulk_payment(
                    bank_service=service,
                    owner_name=owner_name,
                    source_iban=order.source_iban,
                    currency=CURRENCY,
                    payments=[
                        {
                            "amount": line.amount.amount,
                            "destinationIBAN": line.destination_iban,
                            "destinationCreditorName": line.creditor_name,
                            "paymentDescription": line.concept,
                        }
                        for line in lines
                    ],
                    **urls,
                )
        except Exception as e:
            code = e.code if isinstance(e, ArcopayAPIError) else ""
            message = e.message if isinstance(e, ArcopayAPIError) else str(e)
            logger.exception("Arcopay refused bank payment order %s", order.pk)
            order.status = BankPaymentOrderStatus.ERROR
            order.error_code = code[:20]
            order.error_message = message
            order.save(update_fields=["status", "error_code", "error_message"])
            raise ValidationError(_("The bank did not accept the payment: %(error)s") % {"error": message})

        order.arcopay_payment_id = result.get("paymentId", "")
        order.follow_url = result.get("follow", "")
        order.save(update_fields=["arcopay_payment_id", "follow_url"])
        if not order.follow_url:
            raise ValidationError(_("Arcopay did not return a link to sign the payment."))
        return order

    @classmethod
    @transaction.atomic
    def _create_order(cls, user, bank_account, amounts):
        """The pending order and its lines, after checking every invoice with it locked."""
        invoices = list(
            Invoice.objects.select_for_update(of=("self",))
            .select_related("provider")
            .filter(pk__in=[invoice.pk for invoice in amounts])
            .order_by("pk")
        )
        requested = {invoice.pk: amount for invoice, amount in amounts.items()}
        if len(invoices) != len(requested):
            raise ValidationError(_("Some of the invoices no longer exist."))
        pending_ids = cls.invoices_in_pending_orders(requested)
        lines, total = [], Decimal("0")
        for invoice in invoices:
            if invoice.company_id != bank_account.company_id:
                raise ValidationError(
                    _("Invoice %(number)s belongs to another company than the bank account.")
                    % {"number": invoice.invoice_number or invoice.pk}
                )
            reason = cls.ineligibility_reason(invoice, pending_ids)
            if reason:
                raise ValidationError(f"{invoice.invoice_number or invoice.pk}: {reason}")
            amount = Decimal(str(requested[invoice.pk])).quantize(Decimal("0.01"))
            remaining = invoice.amount_remaining.amount
            if amount <= 0 or amount > remaining:
                raise ValidationError(
                    _("%(number)s: the amount must be above zero and at most the %(remaining)s still outstanding.")
                    % {"number": invoice.invoice_number or invoice.pk, "remaining": invoice.amount_remaining}
                )
            destination = cls.destination_account(invoice)
            lines.append(BankPaymentOrderLine(
                invoice=invoice,
                amount=Money(amount, CURRENCY),
                destination_iban=destination.iban.replace(" ", "").upper(),
                creditor_name=_sepa_text(destination.holder_name or invoice.provider.name)[:200],
                concept=cls.concept(invoice),
            ))
            total += amount

        order = BankPaymentOrder.objects.create(
            company=bank_account.company,
            kind=BankPaymentOrderKind.DIRECT if len(lines) == 1 else BankPaymentOrderKind.BULK,
            bank_account=bank_account,
            source_iban=bank_account.iban.replace(" ", "").upper(),
            amount=Money(total, CURRENCY),
            created_by=user,
        )
        for line in lines:
            line.order = order
        BankPaymentOrderLine.objects.bulk_create(lines)
        return order

    # -- following it -------------------------------------------------------------

    @classmethod
    def refresh_status(cls, order):
        """Ask Arcopay where a pending order is and act on it. Returns the order."""
        if not order.is_pending:
            return order
        if not order.arcopay_payment_id:
            # Never reached Arcopay (the process died sending it)
            if timezone.now() - order.created_at > PENDING_EXPIRY:
                cls._close(order, BankPaymentOrderStatus.EXPIRED)
            return order
        result = ArcopayService.get_payment_status(order.arcopay_payment_id)
        return cls.apply_bank_status(order, str(result.get("status", "") or ""))

    @classmethod
    @transaction.atomic
    def apply_bank_status(cls, order, bank_status):
        order = BankPaymentOrder.objects.select_for_update().get(pk=order.pk)
        if not order.is_pending:
            return order
        bank_status = bank_status.strip().upper()
        order.bank_status = bank_status[:20]
        order.status_checked_at = timezone.now()
        order.save(update_fields=["bank_status", "status_checked_at"])

        if bank_status in COMPLETED_BANK_STATUSES:
            cls._complete(order)
        elif bank_status in REJECTED_BANK_STATUSES:
            cls._close(order, BankPaymentOrderStatus.REJECTED)
        elif bank_status in CANCELLED_BANK_STATUSES:
            cls._close(order, BankPaymentOrderStatus.CANCELLED)
        elif timezone.now() - order.created_at > PENDING_EXPIRY:
            cls._close(order, BankPaymentOrderStatus.EXPIRED)
        return order

    @staticmethod
    def _close(order, status):
        order.status = status
        order.save(update_fields=["status"])
        logger.info("Bank payment order %s closed as %s (bank status %r)", order.pk, status, order.bank_status)

    @staticmethod
    def _complete(order):
        """One confirmed payment per line, each expecting its invoice's reference in the bank movement."""
        today = timezone.localdate()
        invoice_type = ContentType.objects.get_for_model(Invoice)
        for line in order.lines.select_related("invoice", "invoice__provider"):
            invoice = line.invoice
            payment = Payment.objects.create(
                company=order.company,
                payment_date=today,
                amount=line.amount,
                direction=PaymentDirection.OUTBOUND,
                method=PaymentMethod.TRANSFER,
                status=PaymentStatus.CONFIRMED,
                bank_account=order.bank_account,
                provider=invoice.provider,
                reference=(invoice.invoice_number or str(invoice.pk))[:100],
                expected_bank_reference=invoice.payment_reference,
                notes=_("Sent from the bank (PSD2), order #%(order)s: %(concept)s")
                % {"order": order.pk, "concept": line.concept},
            )
            # Someone may have registered part of it meanwhile: what doesn't
            # fit stays on the payment as the provider's credit
            applied = min(line.amount.amount, max(invoice.amount_remaining.amount, Decimal("0")))
            if applied > 0:
                PaymentAllocation.objects.create(
                    payment=payment,
                    content_type=invoice_type,
                    object_id=invoice.pk,
                    amount_applied=Money(applied, CURRENCY),
                )
            line.payment = payment
            line.save(update_fields=["payment"])
        order.status = BankPaymentOrderStatus.COMPLETED
        order.completed_at = timezone.now()
        order.save(update_fields=["status", "completed_at"])
        logger.info("Bank payment order %s completed: %s payment(s) registered", order.pk, order.lines.count())

    @classmethod
    def cancel(cls, order, user):
        """
        Ask the bank to cancel a pending order. Returns a URL when the user
        has to confirm the cancellation at the bank, else None.
        """
        if not user.has_perm("banking.can_initiate_bank_payment"):
            raise PermissionDenied
        if not order.is_pending:
            raise ValidationError(_("Only orders awaiting signature can be cancelled."))
        if not order.arcopay_payment_id:
            cls._close(order, BankPaymentOrderStatus.CANCELLED)
            return None
        result = ArcopayService.cancel_payment(order.arcopay_payment_id)
        if str(result.get("status", "")).upper() in CANCELLED_BANK_STATUSES:
            cls.apply_bank_status(order, "CANC")
            return None
        return result.get("follow") or None

    @classmethod
    def poll_pending(cls) -> dict:
        """Refresh every pending order; the periodic safety net when no callback arrives."""
        counts = {"checked": 0, "errors": 0}
        for order in BankPaymentOrder.objects.filter(status=BankPaymentOrderStatus.PENDING):
            try:
                cls.refresh_status(order)
                counts["checked"] += 1
            except Exception:
                counts["errors"] += 1
                logger.exception("Could not refresh bank payment order %s", order.pk)
        return counts
