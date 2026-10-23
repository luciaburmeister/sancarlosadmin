"""Admin screen paying a purchase invoice from another group company (see IntercompanyPaymentService)."""
from decimal import Decimal

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Field, Layout, Row
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from unfold.widgets import (
    UnfoldAdminDateWidget,
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextInputWidget,
)

from company.models import BankAccount
from invoices.models import Invoice
from invoices.services.intercompany_payment_service import IntercompanyPaymentService


class IntercompanyPaymentForm(forms.Form):
    payer_company = forms.ModelChoiceField(
        queryset=None,
        label=_("Paid by"),
        help_text=_("The group company whose bank the money left from. It issues us a debit note for it."),
        widget=UnfoldAdminSelectWidget,
    )
    bank_account = forms.ModelChoiceField(
        queryset=None,
        label=_("From bank account"),
        widget=UnfoldAdminSelectWidget,
    )
    payment_date = forms.DateField(label=_("Payment Date"), widget=UnfoldAdminDateWidget)
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        label=_("Amount"),
        widget=UnfoldAdminDecimalFieldWidget,
    )
    reference = forms.CharField(
        max_length=100,
        required=False,
        label=_("Reference"),
        help_text=_("Defaults to the invoice's payment reference: put it in the transfer concept."),
        widget=UnfoldAdminTextInputWidget,
    )

    def __init__(self, *args, user, invoice, **kwargs):
        super().__init__(*args, **kwargs)
        payers = IntercompanyPaymentService.payer_choices(user, invoice)
        self.fields["payer_company"].queryset = payers
        self.fields["bank_account"].queryset = (
            BankAccount.objects.filter(company__in=payers).select_related("company").order_by("company__name", "pk")
        )
        self.max_amount = invoice.amount_remaining.amount
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.template_pack = "unfold_crispy"
        self.helper.layout = Layout(
            Row(
                Column(Field("payer_company"), css_class="col-span-12 md:col-span-6"),
                Column(Field("bank_account"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4",
            ),
            Row(
                Column(Field("payment_date"), css_class="col-span-12 md:col-span-6"),
                Column(Field("amount"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4",
            ),
            Row(Column(Field("reference"), css_class="col-span-12"), css_class="gap-4 mb-4"),
        )

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount > self.max_amount:
            raise forms.ValidationError(
                _("Cannot pay more than the %(max)s still outstanding.") % {"max": self.max_amount}
            )
        return amount

    def clean(self):
        cleaned = super().clean()
        payer, account = cleaned.get("payer_company"), cleaned.get("bank_account")
        if payer and account and account.company_id != payer.pk:
            self.add_error("bank_account", _("The bank account must belong to the paying company."))
        return cleaned


class InvoiceIntercompanyPaymentView(View):
    model_admin = None
    template_name = "admin/invoices/invoice/intercompany_payment.html"

    def dispatch(self, request, *args, **kwargs):
        # The admin action decides whether to show the button; this stops
        # someone reaching the URL directly
        self.invoice = get_object_or_404(self.model_admin.get_queryset(request), pk=kwargs["object_id"])
        if not IntercompanyPaymentService.can_register(request.user, self.invoice):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, object_id):
        form = IntercompanyPaymentForm(
            user=request.user,
            invoice=self.invoice,
            initial={
                "payment_date": timezone.localdate(),
                "amount": self.invoice.amount_remaining.amount,
                "reference": self.invoice.payment_reference,
            },
        )
        return self._render(request, form)

    def post(self, request, object_id):
        form = IntercompanyPaymentForm(request.POST, user=request.user, invoice=self.invoice)
        if form.is_valid():
            data = form.cleaned_data
            try:
                record = IntercompanyPaymentService.create(
                    user=request.user,
                    source_invoice=self.invoice,
                    payer_company=data["payer_company"],
                    bank_account=data["bank_account"],
                    payment_date=data["payment_date"],
                    amount=data["amount"],
                    reference=data["reference"] or None,
                )
            except ValidationError as e:
                form.add_error(None, e)
            else:
                self.model_admin.message_user(
                    request,
                    _("Paid by %(company)s: debit note %(number)s registered as a debt to it.")
                    % {"company": record.payer_company, "number": record.debit_note.invoice_number},
                )
                return redirect(reverse("admin:invoices_invoice_change", args=[self.invoice.pk]))
        return self._render(request, form)

    def _render(self, request, form):
        context = {
            **self.model_admin.admin_site.each_context(request),
            "form": form,
            "document": self.invoice,
            "document_total": self.invoice.total_amount,
            "document_remaining": self.invoice.amount_remaining,
            "cancel_url": reverse("admin:invoices_invoice_change", args=[self.invoice.pk]),
            "title": _("Pay from another company"),
            "opts": Invoice._meta,
        }
        return TemplateResponse(request, self.template_name, context)
