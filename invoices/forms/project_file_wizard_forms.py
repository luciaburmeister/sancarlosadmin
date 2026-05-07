from django import forms
from django.utils.translation import gettext_lazy as _
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Row, Column, Field, HTML, Div
from django.conf import settings
from decimal import Decimal

from invoices.models import SalesInvoice, TaxType
from management.models import ProjectFile
from sales.models import Client
from company.models import Company
from unfold.widgets import (
    UnfoldAdminSelectWidget,
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminDateWidget,
    UnfoldAdminTextareaWidget,
)


class ProjectFileInvoiceSetupFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(
            HTML(f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4 dark:bg-gray-900 dark:border-gray-700">
                    <h3 class="text-base font-semibold text-gray-800 dark:text-gray-200 mb-3">{_('Invoice Setup')}</h3>"""),
            Row(
                Column(Field('company'), css_class="col-span-12 md:col-span-6"),
                Column(Field('date_issued'), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            Row(
                Column(Field('due_date'), css_class="col-span-12 md:col-span-6"),
                Column(Field('invoice_mode'), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            Row(
                Column(Field('percentage'), css_class="col-span-12 md:col-span-6"),
                Column(Field('custom_amount'), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            HTML("</div>")
        )


class ProjectFileInvoiceSetupForm(forms.Form):
    INVOICE_MODES = [
        ('percentage', _('By Percentage')),
        ('custom', _('Custom Amount')),
    ]

    company = forms.ModelChoiceField(
        queryset=Company.objects.all(),
        widget=UnfoldAdminSelectWidget,
        required=True,
        label=_("Company")
    )
    date_issued = forms.DateField(
        widget=UnfoldAdminDateWidget,
        label=_("Date Issued"),
        required=True,
    )
    due_date = forms.DateField(
        widget=UnfoldAdminDateWidget,
        label=_("Due Date"),
        required=False,
    )
    invoice_mode = forms.ChoiceField(
        choices=INVOICE_MODES,
        initial='percentage',
        widget=UnfoldAdminSelectWidget,
        label=_("Invoice Mode"),
    )
    percentage = forms.DecimalField(
        max_digits=6,
        decimal_places=2,
        initial=Decimal("100.00"),
        widget=UnfoldAdminDecimalFieldWidget,
        label=_("Percentage (%)"),
        required=False,
        help_text=_("Percentage of the remaining invoiceable amount"),
    )
    custom_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=UnfoldAdminDecimalFieldWidget,
        label=_("Custom Amount (Subtotal)"),
        required=False,
        help_text=_("Custom subtotal amount for this invoice"),
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.project_file = kwargs.pop("project_file", None)
        self.invoicing_summary = kwargs.pop("invoicing_summary", None)
        super().__init__(*args, **kwargs)
        self.helper = ProjectFileInvoiceSetupFormHelper()

        if self.project_file and self.project_file.company:
            self.fields['company'].initial = self.project_file.company

        from core.admin.company_access import get_user_companies
        if self.request and self.request.user and not self.request.user.is_superuser:
            user_companies = get_user_companies(self.request.user)
            self.fields["company"].queryset = user_companies

    def clean(self):
        cleaned_data = super().clean()
        mode = cleaned_data.get('invoice_mode')
        percentage = cleaned_data.get('percentage')
        custom_amount = cleaned_data.get('custom_amount')

        if not self.invoicing_summary:
            return cleaned_data

        remaining_subtotal = self.invoicing_summary['remaining_subtotal']

        if mode == 'percentage':
            if not percentage or percentage <= 0:
                self.add_error('percentage', _("Please enter a valid percentage."))
            elif percentage > Decimal("100.00"):
                self.add_error('percentage', _("Percentage cannot exceed 100%."))
            else:
                calculated_subtotal = (remaining_subtotal * percentage / Decimal("100")).quantize(Decimal("0.01"))
                if calculated_subtotal > remaining_subtotal + Decimal("0.01"):
                    self.add_error('percentage', _(
                        "This percentage would exceed the remaining invoiceable amount (%(remaining)s)."
                    ) % {'remaining': remaining_subtotal})
                cleaned_data['_calculated_subtotal'] = calculated_subtotal

        elif mode == 'custom':
            if not custom_amount or custom_amount <= 0:
                self.add_error('custom_amount', _("Please enter a valid amount."))
            elif custom_amount > remaining_subtotal + Decimal("0.01"):
                self.add_error('custom_amount', _(
                    "Amount cannot exceed the remaining invoiceable subtotal (%(remaining)s)."
                ) % {'remaining': remaining_subtotal})
            else:
                cleaned_data['_calculated_subtotal'] = custom_amount

        return cleaned_data

    def get_calculated_subtotal(self):
        """Returns the calculated subtotal after form validation."""
        return self.cleaned_data.get('_calculated_subtotal', Decimal("0.00"))


class ProjectFileInvoiceNotesFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(
            HTML(f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4 dark:bg-gray-900 dark:border-gray-700">
                    <h3 class="text-base font-semibold text-gray-800 dark:text-gray-200 mb-3">{_('Notes')}</h3>"""),
            Row(
                Column(Field('notes'), css_class="col-span-12"),
            ),
            HTML("</div>")
        )


class ProjectFileInvoiceNotesForm(forms.Form):
    notes = forms.CharField(
        widget=UnfoldAdminTextareaWidget,
        required=False,
        label=_("Notes"),
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self.helper = ProjectFileInvoiceNotesFormHelper()
