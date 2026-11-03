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
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from djmoney.money import Money
from moneyed import CURRENCIES
from unfold.widgets import (
    UnfoldAdminDateWidget,
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminFileFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextInputWidget,
)

from company.models import BankAccount
from payments.models import (
    MANUAL_PAYMENT_METHODS,
    Payment,
    PaymentAllocation,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)


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
    number = forms.CharField(
        max_length=50,
        required=False,
        label=_("Note Number"),
        help_text=_("The number printed on the promissory note."),
        widget=UnfoldAdminTextInputWidget,
    )
    due_date = forms.DateField(
        required=False,
        label=_("Note Due Date"),
        widget=UnfoldAdminDateWidget,
    )
    bank_account = forms.ModelChoiceField(
        queryset=BankAccount.objects.none(),
        required=False,
        label=_("Note Bank Account"),
        help_text=_("Where the note will be charged at its due date."),
        widget=UnfoldAdminSelectWidget,
    )
    note_document = forms.FileField(
        required=False,
        label=_("Note Document"),
        widget=UnfoldAdminFileFieldWidget,
    )

    def __init__(self, *args, max_amount=None, company=None, direction=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = MANUAL_PAYMENT_METHODS
        self.fields["bank_account"].queryset = (
            BankAccount.objects.filter(company=company) if company else BankAccount.objects.none()
        )
        self.max_amount = max_amount
        self.direction = direction
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
                Column(
                    Field("method", **{"@change": "method = $event.target.value"}),
                    css_class="col-span-12 md:col-span-6",
                ),
                css_class="gap-4 mb-4",
            ),
            Row(
                Column(Field("reference"), css_class="col-span-12"),
                css_class="gap-4 mb-4",
            ),
            Row(
                Column(Field("number"), css_class="col-span-12 md:col-span-6"),
                Column(Field("due_date"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4",
                **{"x-show": "method === 'promissory_note'"},
            ),
            Row(
                Column(Field("bank_account"), css_class="col-span-12 md:col-span-6"),
                Column(Field("note_document"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4",
                **{"x-show": "method === 'promissory_note'"},
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

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("method") != PaymentMethod.PROMISSORY_NOTE:
            return cleaned
        if not cleaned.get("number"):
            self.add_error("number", _("Required for a promissory note."))
        if not cleaned.get("due_date"):
            self.add_error("due_date", _("Required for a promissory note."))
        if self.direction == PaymentDirection.OUTBOUND and not cleaned.get("bank_account"):
            self.add_error("bank_account", _("Required for a promissory note issued to a provider."))
        return cleaned


class RegisterPaymentView(View):
    """Subclass and set `model`, `direction` and `change_url_name`."""

    model_admin = None
    model = None
    direction = None
    change_url_name = None
    template_name = "admin/payments/register_payment.html"
    distribute_surplus_template_name = "admin/payments/distribute_surplus.html"
    allow_surplus_distribution = False
    distribute_surplus_url_name = None
    apply_credit_url_name = None

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

    def get_applied_amount(self, document, money):
        """The portion of the entered amount applied to this document, capped at what it still owes."""
        remaining = document.amount_remaining
        if money.currency != remaining.currency or money <= remaining:
            return money
        if remaining.amount <= 0:
            return Money(0, money.currency)
        return remaining

    def get_cancel_url(self, document):
        return reverse(self.change_url_name, args=[document.pk])

    def get_counterparty_filters(self, document):
        """Return the invoice_filters narrowing another document to the same counterparty as `document`."""
        raise NotImplementedError

    def get_counterparty_label(self, document):
        """Return the provider/client name `document` belongs to, for the surplus screen's header."""
        raise NotImplementedError

    def get_surplus_candidates(self, document, currency):
        from payments.mixins import pending_invoices_queryset

        filters = {**self.get_counterparty_filters(document), "company": document.company}
        return (
            pending_invoices_queryset(self.model, filters)
            .exclude(pk=document.pk)
            .filter(total_amount_currency=str(currency))
        )

    def get_counterparty_credit(self, document):
        """Total unallocated credit this document's counterparty has, in the document's currency, or None."""
        from django.db.models import Sum

        from payments.mixins import unallocated_payments_q

        filters = {**self.get_counterparty_filters(document), "company": document.company}
        total = (
            Payment.objects.filter(**filters)
            .filter(unallocated_payments_q(self.direction))
            .filter(amount_pending_allocation_currency=document.total_amount_currency)
            .aggregate(total=Sum("amount_pending_allocation"))["total"]
        )
        if not total:
            return None
        return Money(total, document.total_amount_currency)

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
            company=document.company,
            direction=self.direction,
        )
        return self._render(request, document, form)

    def post(self, request, object_id):
        document = self.get_document(object_id)
        form = RegisterPaymentForm(
            request.POST, request.FILES,
            max_amount=self.get_max_amount(document),
            company=document.company,
            direction=self.direction,
        )

        if form.is_valid():
            data = form.cleaned_data
            money = Money(data["amount"], data["currency"])
            applied_amount = self.get_applied_amount(document, money)
            surplus = money - applied_amount

            if self.allow_surplus_distribution and surplus.amount > 0:
                return self._render_distribute_surplus(request, document, form, applied_amount, surplus)

            if data["method"] == PaymentMethod.PROMISSORY_NOTE:
                from payments.services.promissory_note_service import PromissoryNoteService

                PromissoryNoteService.issue(
                    document=document,
                    money=money,
                    applied_amount=applied_amount,
                    number=data["number"],
                    issue_date=data["payment_date"],
                    due_date=data["due_date"],
                    bank_account=data.get("bank_account"),
                    note_document=data.get("note_document"),
                    reference=data["reference"] or None,
                )
            else:
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
                    PaymentAllocation.objects.create(
                        payment=payment,
                        content_type=ContentType.objects.get_for_model(document),
                        object_id=document.pk,
                        amount_applied=applied_amount,
                    )
            if self.model_admin:
                self.model_admin.message_user(request, _("Payment registered successfully."))
            return redirect(self.get_cancel_url(document))

        return self._render(request, document, form)

    def _render(self, request, document, form):
        admin_site = self.model_admin.admin_site if self.model_admin else None
        counterparty_credit = self.get_counterparty_credit(document) if self.apply_credit_url_name else None
        counterparty_label = self.get_counterparty_label(document) if self.apply_credit_url_name else None
        context = {
            **(admin_site.each_context(request) if admin_site else {}),
            "form": form,
            "document": document,
            "document_total": self.get_document_total(document),
            "document_remaining": document.amount_remaining,
            "cancel_url": self.get_cancel_url(document),
            "title": _("Register Payment"),
            "opts": self.model._meta,
            "counterparty_label": counterparty_label,
            "counterparty_credit": counterparty_credit,
            "apply_credit_url": reverse(self.apply_credit_url_name, args=[document.pk]) if counterparty_credit else None,
        }
        return TemplateResponse(request, self.template_name, context)

    def get_candidate_label(self, invoice):
        return invoice.invoice_number or f"#{invoice.pk}"

    def _render_distribute_surplus(self, request, document, form, applied_amount, surplus):
        today = timezone.localdate()
        candidates = self.get_surplus_candidates(document, surplus.currency)
        candidates_data = [
            {
                "id": invoice.pk,
                "number": self.get_candidate_label(invoice),
                "reference": getattr(invoice, "reference", "") or "",
                "due_date": invoice.due_date,
                "remaining": invoice.db_amount_remaining,
                "is_overdue": bool(invoice.due_date and invoice.due_date < today),
            }
            for invoice in candidates
        ]
        admin_site = self.model_admin.admin_site if self.model_admin else None
        context = {
            **(admin_site.each_context(request) if admin_site else {}),
            "step1_data": form.cleaned_data,
            "payment_money": Money(form.cleaned_data["amount"], form.cleaned_data["currency"]),
            "document": document,
            "document_label": self.get_candidate_label(document),
            "counterparty_label": self.get_counterparty_label(document),
            "document_total": self.get_document_total(document),
            "applied_amount": applied_amount,
            "surplus": surplus,
            "candidates_data": candidates_data,
            "confirm_url": reverse(self.distribute_surplus_url_name, args=[document.pk]),
            "cancel_url": self.get_cancel_url(document),
            "title": _("Distribute Surplus"),
            "opts": self.model._meta,
        }
        return TemplateResponse(request, self.distribute_surplus_template_name, context)
