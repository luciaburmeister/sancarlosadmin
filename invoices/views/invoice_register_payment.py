from decimal import Decimal

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row, Field
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
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

from invoices.models import Invoice
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod, PaymentStatus


class RegisterInvoicePaymentForm(forms.Form):
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
        choices=PaymentMethod.choices,
        label=_("Method"),
        widget=UnfoldAdminSelectWidget,
    )
    reference = forms.CharField(
        max_length=100,
        required=False,
        label=_("Reference"),
        widget=UnfoldAdminTextInputWidget,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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


class InvoiceRegisterPaymentView(View):
    model_admin = None

    def get(self, request, object_id):
        invoice = get_object_or_404(Invoice, pk=object_id)
        form = RegisterInvoicePaymentForm(
            initial={"currency": invoice.total_amount_currency if invoice.total_amount else "EUR"}
        )
        return self._render(request, invoice, form)

    def post(self, request, object_id):
        invoice = get_object_or_404(Invoice, pk=object_id)
        form = RegisterInvoicePaymentForm(request.POST)

        if form.is_valid():
            data = form.cleaned_data
            with transaction.atomic():
                payment = Payment.objects.create(
                    company=invoice.company,
                    payment_date=data["payment_date"],
                    amount=Money(data["amount"], data["currency"]),
                    direction=PaymentDirection.OUTBOUND,
                    method=data["method"],
                    status=PaymentStatus.CONFIRMED,
                    reference=data["reference"] or None,
                    amount_pending_allocation=Money(data["amount"], data["currency"]),
                )
                PaymentAllocation.objects.create(
                    payment=payment,
                    content_type=ContentType.objects.get_for_model(invoice),
                    object_id=invoice.pk,
                    amount_applied=Money(data["amount"], data["currency"]),
                )
            if self.model_admin:
                self.model_admin.message_user(request, _("Payment registered successfully."))
            return redirect(reverse("admin:invoices_invoice_change", args=[object_id]))

        return self._render(request, invoice, form)

    def _render(self, request, invoice, form):
        admin_site = self.model_admin.admin_site if self.model_admin else None
        context = {
            **(admin_site.each_context(request) if admin_site else {}),
            "form": form,
            "invoice": invoice,
            "title": _("Register Payment"),
            "opts": Invoice._meta,
        }
        return TemplateResponse(
            request,
            "admin/invoices/invoice/register_payment.html",
            context,
        )
