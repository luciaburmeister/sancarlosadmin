"""Shared "Register Payment" admin screen for any document that receives payments.

A `Payable` document (Invoice, SalesInvoice, ExpenseClaim, ...) gets the screen
by subclassing `RegisterPaymentView` and declaring its model, the direction of
the money and where to go back to. The form and the create-payment-then-allocate
transaction are identical for every document, so they live here once.
"""
from decimal import Decimal

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Field, Layout, Row
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from djmoney.money import Money
from moneyed import CURRENCIES
from unfold.widgets import (
    UnfoldAdminDateWidget,
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextInputWidget,
)

from payments.models import Payment, PaymentAllocation, PaymentStatus


class RegisterPaymentForm(forms.Form):
    _currency_choices = sorted([(code, code) for code in CURRENCIES])

    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        label=_("Amount"),
        widget=UnfoldAdminDecimalFieldWidget,
    )
    currency = forms.ChoiceField(
        choices=_currency_choices,
        initial="EUR",
        label=_("Currency"),
        widget=UnfoldAdminSelectWidget,
    )
    payment_date = forms.DateField(
        label=_("Payment Date"),
        widget=UnfoldAdminDateWidget,
    )
    method = forms.ChoiceField(
        choices=[],
        label=_("Method"),
        widget=UnfoldAdminSelectWidget,
    )
    reference = forms.CharField(
        max_length=100,
        required=False,
        label=_("Reference"),
        widget=UnfoldAdminTextInputWidget,
    )

    def __init__(self, *args, max_amount=None, **kwargs):
        super().__init__(*args, **kwargs)
        from payments.models import PaymentMethod

        self.fields["method"].choices = PaymentMethod.choices
        self.max_amount = max_amount
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.template_pack = "unfold_crispy"
        self.helper.layout = Layout(
            Row(
                Column(Field("amount"), css_class="col-span-12 md:col-span-6"),
                Column(Field("currency"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4",
            ),
            Row(
                Column(Field("payment_date"), css_class="col-span-12 md:col-span-6"),
                Column(Field("method"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4",
            ),
            Row(
                Column(Field("reference"), css_class="col-span-12"),
                css_class="gap-4 mb-4",
            ),
        )

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        # Only enforced by documents that opt in via `max_amount` - invoices
        # deliberately allow over-allocation, expense claims do not.
        if self.max_amount is not None and amount > self.max_amount:
            raise forms.ValidationError(
                _("Cannot pay more than the %(max)s still outstanding.") % {"max": self.max_amount}
            )
        return amount


class RegisterPaymentView(View):
    """Subclass and set `model`, `direction` and `change_url_name`."""

    model_admin = None
    model = None
    direction = None
    change_url_name = None
    template_name = "admin/payments/register_payment.html"

    # ── Hooks ────────────────────────────────────────────────────────────────

    def get_document(self, object_id):
        return get_object_or_404(self.model, pk=object_id)

    def get_document_total(self, document):
        """The document's face value, shown in the header."""
        return document.total_amount

    def get_default_currency(self, document):
        total = self.get_document_total(document)
        return total.currency if total else "EUR"

    def get_max_amount(self, document):
        """Return a cap to validate the amount against, or None for no cap."""
        return None

    def get_cancel_url(self, document):
        return reverse(self.change_url_name, args=[document.pk])

    def can_register_payment(self, request, document):
        """
        Override to restrict who may pay, and when.

        The admin action that links here does its own check to decide whether
        to show the button; this is what stops someone reaching the URL
        directly, so any rule enforced there belongs here too.
        """
        return True

    # ── Request handling ─────────────────────────────────────────────────────

    def dispatch(self, request, *args, **kwargs):
        object_id = kwargs.get("object_id")
        if object_id is not None and not self.can_register_payment(request, self.get_document(object_id)):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, object_id):
        document = self.get_document(object_id)
        form = RegisterPaymentForm(
            initial={"currency": str(self.get_default_currency(document))},
            max_amount=self.get_max_amount(document),
        )
        return self._render(request, document, form)

    def post(self, request, object_id):
        document = self.get_document(object_id)
        form = RegisterPaymentForm(request.POST, max_amount=self.get_max_amount(document))

        if form.is_valid():
            data = form.cleaned_data
            money = Money(data["amount"], data["currency"])
            with transaction.atomic():
                payment = Payment.objects.create(
                    company=document.company,
                    payment_date=data["payment_date"],
                    amount=money,
                    direction=self.direction,
                    method=data["method"],
                    status=PaymentStatus.CONFIRMED,
                    reference=data["reference"] or None,
                    amount_pending_allocation=money,
                )
                # Saving the allocation recalculates the payment's pending
                # amount and the document's payment status, in one transaction.
                PaymentAllocation.objects.create(
                    payment=payment,
                    content_type=ContentType.objects.get_for_model(document),
                    object_id=document.pk,
                    amount_applied=money,
                )
            if self.model_admin:
                self.model_admin.message_user(request, _("Payment registered successfully."))
            return redirect(self.get_cancel_url(document))

        return self._render(request, document, form)

    def _render(self, request, document, form):
        admin_site = self.model_admin.admin_site if self.model_admin else None
        context = {
            **(admin_site.each_context(request) if admin_site else {}),
            "form": form,
            "document": document,
            "document_total": self.get_document_total(document),
            "document_remaining": document.amount_remaining,
            "cancel_url": self.get_cancel_url(document),
            "title": _("Register Payment"),
            "opts": self.model._meta,
        }
        return TemplateResponse(request, self.template_name, context)
