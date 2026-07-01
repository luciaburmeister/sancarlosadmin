from django import forms
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminTextareaWidget
from company.models import Company
from invoices.models import InvoicePaymentStatus


ACTION_CHOICES = [
    (InvoicePaymentStatus.PAID, _("Mark as Paid")),
    (InvoicePaymentStatus.UNPAID, _("Mark as Unpaid")),
]


class BulkMarkInvoicePaidForm(forms.Form):
    company = forms.ModelChoiceField(
        queryset=Company.objects.all(),
        label=_("Company"),
        widget=UnfoldAdminSelectWidget(),
        help_text=_("Select the company these invoices belong to."),
    )
    action = forms.ChoiceField(
        choices=ACTION_CHOICES,
        label=_("Action"),
        widget=UnfoldAdminSelectWidget(),
        help_text=_("Choose the payment status to apply to the listed invoices."),
    )
    invoice_numbers = forms.CharField(
        label=_("Invoice Numbers"),
        widget=UnfoldAdminTextareaWidget(attrs={
            "rows": 10,
            "placeholder": _("Enter one invoice number per line, e.g.:\nFRA-2026/001\nFRA-2026/002\nFRA-2026/003"),
        }),
        help_text=_(
            "Paste one invoice number per line."
        ),
    )

    class Media:
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/core.js",
        ]