from django import forms
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminTextareaWidget
from company.models import Company


class BulkMarkPaidForm(forms.Form):
    company = forms.ModelChoiceField(
        queryset=Company.objects.all(),
        label=_("Company"),
        widget=UnfoldAdminSelectWidget(),
        help_text=_("Select the company these invoices belong to."),
    )
    invoice_numbers = forms.CharField(
        label=_("Invoice Numbers"),
        widget=UnfoldAdminTextareaWidget(attrs={
            "rows": 10,
            "placeholder": _("Enter one invoice number per line, e.g.:\nFV-2026/001\nFV-2026/002\nFV-2026/003"),
        }),
        help_text=_(
            "Paste one invoice number per line. "
            "Only Final invoices that are not already Paid will be updated."
        ),
    )

    class Media:
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/core.js",
        ]
