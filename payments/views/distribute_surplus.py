"""Confirms a payment whose amount exceeds a document's remaining balance.

Reached only by submitting the form `RegisterPaymentView` renders when it
detects a surplus. Nothing from step 1 is persisted until this single POST:
it re-validates the payment fields, locks the original document plus every
invoice the user chose to apply the surplus to, and creates the Payment and
all its allocations in one transaction.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from payments.models import Payment, PaymentAllocation, PaymentMethod, PaymentStatus
from payments.views.register_payment import RegisterPaymentForm, RegisterPaymentView


class DistributeSurplusView(RegisterPaymentView):
    def get(self, request, object_id):
        document = self.get_document(object_id)
        return redirect(self.get_cancel_url(document))

    def post(self, request, object_id):
        document = self.get_document(object_id)
        form = RegisterPaymentForm(
            request.POST, request.FILES,
            max_amount=self.get_max_amount(document),
            company=document.company,
            direction=self.direction,
        )
        if not form.is_valid():
            return redirect(self.get_cancel_url(document))

        data = form.cleaned_data
        money = Money(data["amount"], data["currency"])

        with transaction.atomic():
            locked_document = self.model.objects.select_for_update().get(pk=document.pk)
            applied_amount = self.get_applied_amount(locked_document, money)

            candidate_ids = list(
                self.get_surplus_candidates(locked_document, money.currency).values_list("pk", flat=True)
            )
            locked_candidates = self.model.objects.select_for_update().filter(pk__in=candidate_ids)

            if data["method"] == PaymentMethod.PROMISSORY_NOTE:
                from payments.services.promissory_note_service import PromissoryNoteService

                note = PromissoryNoteService.issue(
                    document=locked_document,
                    money=money,
                    applied_amount=applied_amount,
                    number=data["number"],
                    issue_date=data["payment_date"],
                    due_date=data["due_date"],
                    bank_account=data.get("bank_account"),
                    note_document=data.get("note_document"),
                    reference=data["reference"] or None,
                )
                payment = note.payment
            else:
                payment = Payment.objects.create(
                    company=locked_document.company,
                    payment_date=data["payment_date"],
                    amount=money,
                    direction=self.direction,
                    method=data["method"],
                    status=PaymentStatus.CONFIRMED,
                    reference=data["reference"] or None,
                    amount_pending_allocation=money,
                )
                PaymentAllocation.objects.create(
                    payment=payment,
                    content_type=ContentType.objects.get_for_model(locked_document),
                    object_id=locked_document.pk,
                    amount_applied=applied_amount,
                )

            remaining_surplus = (money - applied_amount).amount
            for invoice in locked_candidates:
                if remaining_surplus <= 0:
                    break
                requested = self._requested_amount(request, invoice.pk)
                if requested is None or requested <= 0:
                    continue
                capped = min(requested, remaining_surplus)
                allocation_money = self.get_applied_amount(invoice, Money(capped, money.currency))
                if allocation_money.amount <= 0:
                    continue
                PaymentAllocation.objects.create(
                    payment=payment,
                    content_type=ContentType.objects.get_for_model(invoice),
                    object_id=invoice.pk,
                    amount_applied=allocation_money,
                )
                remaining_surplus -= allocation_money.amount

        if self.model_admin:
            self.model_admin.message_user(request, _("Payment registered successfully."))
        return redirect(self.get_cancel_url(locked_document))

    @staticmethod
    def _requested_amount(request, invoice_pk):
        raw = request.POST.get(f"alloc_{invoice_pk}", "").strip()
        if not raw:
            return None
        try:
            return Decimal(raw)
        except InvalidOperation:
            return None
