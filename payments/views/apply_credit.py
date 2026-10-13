"""Applies a counterparty's unallocated credit to one of their unpaid documents.

The inverse of DistributeSurplusView: there, one payment's surplus spreads
across many documents; here, many payments' leftover credit converge on one
document. Nothing is persisted until the single POST - it locks the document
plus every payment the user drew from, re-validates each amount against real
balances, and creates one PaymentAllocation per payment used.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from djmoney.money import Money

from payments.mixins import unallocated_payments_q
from payments.models import Payment, PaymentAllocation


class ApplyCreditView(View):
    """Subclass and set `model`, `direction` and `change_url_name` (see RegisterPaymentView)."""

    model_admin = None
    model = None
    direction = None
    change_url_name = None
    template_name = "admin/payments/apply_credit.html"

    def get_document(self, object_id):
        return get_object_or_404(self.model, pk=object_id)

    def get_candidate_label(self, document):
        return document.invoice_number or f"#{document.pk}"

    def get_counterparty_filters(self, document):
        raise NotImplementedError

    def get_counterparty_label(self, document):
        raise NotImplementedError

    def get_credit_candidates(self, document):
        filters = {**self.get_counterparty_filters(document), "company": document.company}
        return (
            Payment.objects.filter(**filters)
            .filter(unallocated_payments_q(self.direction))
            .filter(amount_pending_allocation_currency=document.total_amount_currency)
            .order_by("payment_date", "pk")
        )

    def get_cancel_url(self, document):
        return reverse(self.change_url_name, args=[document.pk])

    def can_apply_credit(self, request, document):
        """
        Mirrors the admin action's own visibility check, which decides whether
        the button is shown - without this the URL would still be reachable by
        anyone who typed it even when the invoice has no credit to draw from.
        """
        from invoices.models import InvoicePaymentStatus

        if document.payment_status not in (InvoicePaymentStatus.UNPAID, InvoicePaymentStatus.PARTIAL):
            return False
        return self.get_credit_candidates(document).exists()

    def dispatch(self, request, *args, **kwargs):
        object_id = kwargs.get("object_id")
        if object_id is not None and not self.can_apply_credit(request, self.get_document(object_id)):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, object_id):
        document = self.get_document(object_id)
        candidates_data = [
            {
                "id": payment.pk,
                "date": payment.payment_date,
                "reference": payment.reference or "",
                "method": payment.get_method_display(),
                "available": payment.amount_pending_allocation.amount,
            }
            for payment in self.get_credit_candidates(document)
        ]
        admin_site = self.model_admin.admin_site if self.model_admin else None
        context = {
            **(admin_site.each_context(request) if admin_site else {}),
            "document": document,
            "document_label": self.get_candidate_label(document),
            "counterparty_label": self.get_counterparty_label(document),
            "document_remaining": document.amount_remaining,
            "candidates_data": candidates_data,
            "confirm_url": request.path,
            "cancel_url": self.get_cancel_url(document),
            "title": _("Apply Credit"),
            "opts": self.model._meta,
        }
        return TemplateResponse(request, self.template_name, context)

    def post(self, request, object_id):
        document = self.get_document(object_id)
        content_type = ContentType.objects.get_for_model(self.model)

        with transaction.atomic():
            locked_document = self.model.objects.select_for_update().get(pk=document.pk)
            remaining = locked_document.amount_remaining_decimal

            candidate_ids = list(self.get_credit_candidates(locked_document).values_list("pk", flat=True))
            locked_payments = (
                Payment.objects.select_for_update()
                .filter(pk__in=candidate_ids)
                .order_by("payment_date", "pk")
            )

            for payment in locked_payments:
                if remaining <= 0:
                    break
                requested = self._requested_amount(request, payment.pk)
                if requested is None or requested <= 0:
                    continue
                capped = min(requested, remaining, payment.amount_pending_allocation.amount)
                if capped <= 0:
                    continue
                PaymentAllocation.objects.create(
                    payment=payment,
                    content_type=content_type,
                    object_id=locked_document.pk,
                    amount_applied=Money(capped, payment.amount_pending_allocation_currency),
                )
                remaining -= capped

        if self.model_admin:
            self.model_admin.message_user(request, _("Credit applied successfully."))
        return redirect(self.get_cancel_url(document))

    @staticmethod
    def _requested_amount(request, payment_pk):
        raw = request.POST.get(f"use_{payment_pk}", "").strip()
        if not raw:
            return None
        try:
            return Decimal(raw)
        except InvalidOperation:
            return None
