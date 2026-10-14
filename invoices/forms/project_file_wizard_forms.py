from django import forms
from django.utils.translation import gettext_lazy as _
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Row, Column, Field, HTML, Div
from django.conf import settings
from decimal import Decimal

from invoices.models import PaymentMethod, SalesInvoice, TaxType
from management.models import ProjectFile
from sales.models import Client
from company.models import Company
from unfold.widgets import (
    UnfoldAdminSelectWidget,
    UnfoldBooleanSwitchWidget,
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
            Row(
                Column(Field('payment_method'), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            HTML("</div>")
        )


class ProjectFileInvoiceSetupForm(forms.Form):
    INVOICE_MODES = [
        ('full', _('Everything pending')),
        ('percentage', _('By Percentage')),
        ('custom', _('Custom Amount')),
        ('advance', _('Advance invoice (% of the project file)')),
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
        initial='full',
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
    payment_method = forms.ChoiceField(
        choices=PaymentMethod.choices,
        initial=PaymentMethod.TRANSFER,
        widget=UnfoldAdminSelectWidget,
        label=_("Payment Method"),
        required=True,
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.project_file = kwargs.pop("project_file", None)
        self.invoicing_summary = kwargs.pop("invoicing_summary", None)
        super().__init__(*args, **kwargs)
        self.helper = ProjectFileInvoiceSetupFormHelper()
        # An advance's percentage is of the whole net base, not of what is
        # left: the page swaps these in when the advance mode is picked
        self.fields['percentage'].widget.attrs.update({
            'data-advance-label': _("Percentage of the project file's taxable base (%)"),
            'data-advance-help': _(
                "Applied to the project file's whole net taxable base, not to what is left to invoice."
            ),
        })
        self.fields['custom_amount'].widget.attrs.update({
            'data-advance-label': _("Advance base (€)"),
            'data-advance-help': _(
                "Taxable base of the advance invoice; its percentage is derived from it."
            ),
        })

        if self.project_file and self.project_file.company:
            self.fields['company'].initial = self.project_file.company

        if self.project_file and self.project_file.client:
            self.fields['payment_method'].initial = (
                self.project_file.client.payment_method or PaymentMethod.TRANSFER
            )

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

        if mode == 'advance':
            self._clean_advance(cleaned_data)
            return cleaned_data

        # Goods wait until no advance is a pro forma or being cancelled at
        # the AEAT (another advance may still be issued)
        from invoices.services.advances import AdvanceInvoiceService

        advance_error = AdvanceInvoiceService.goods_block_error(self.invoicing_summary)
        if advance_error:
            self.add_error('invoice_mode', advance_error)
            return cleaned_data
        # Only the advances' company can deduct them from the goods
        company = cleaned_data.get('company')
        if (
            self.invoicing_summary['advances_open_net'] > Decimal("0.01")
            and company is not None and company != self.project_file.company
        ):
            self.add_error('company', _(
                "This project file has advances to deduct: its goods are invoiced by its company "
                "(%(company)s)."
            ) % {'company': self.project_file.company})
            return cleaned_data

        remaining_subtotal = self.invoicing_summary['remaining_subtotal']

        if mode == 'full':
            if not self.invoicing_summary['can_invoice']:
                self.add_error('invoice_mode', _("This project file has nothing left to invoice."))
            cleaned_data['_calculated_subtotal'] = remaining_subtotal

        elif mode == 'percentage':
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

    def _clean_advance(self, cleaned_data):
        """Advance mode: the invoice is the project file's company's, and
        AdvanceInvoiceService must be able to issue the advance entered."""
        from invoices.services.advances import AdvanceInvoiceService

        company = cleaned_data.get('company')
        if company and company != self.project_file.company:
            self.add_error('company', _(
                "An advance invoice is issued by the project file's company (%(company)s)."
            ) % {'company': self.project_file.company})

        if 'percentage' in self.errors or 'custom_amount' in self.errors:
            return
        advance_input = self.advance_input(cleaned_data)
        preview = AdvanceInvoiceService.preview(
            self.project_file, summary=self.invoicing_summary, **advance_input
        )
        if preview['error']:
            field = 'custom_amount' if 'amount' in advance_input else 'percentage'
            self.add_error(field, preview['error'])

    @staticmethod
    def advance_input(cleaned_data):
        """The advance entered in advance mode: its amount (the advance's
        base) when one is given, else its percentage."""
        if cleaned_data.get('custom_amount') is not None:
            return {'amount': cleaned_data['custom_amount']}
        return {'percentage': cleaned_data.get('percentage')}

    def get_calculated_subtotal(self):
        """Returns the calculated subtotal after form validation."""
        return self.cleaned_data.get('_calculated_subtotal', Decimal("0.00"))


class ProjectFileInvoiceNotesFormHelper(FormHelper):
    def __init__(self, *args, advance=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        rows = [
            Row(
                Column(Field('notes'), css_class="col-span-12"),
            ),
        ]
        if advance:
            rows.append(Row(
                Column(Field('finalize'), css_class="col-span-12"),
                css_class="mt-4",
            ))
        self.layout = Layout(
            HTML(f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4 dark:bg-gray-900 dark:border-gray-700">
                    <h3 class="text-base font-semibold text-gray-800 dark:text-gray-200 mb-3">{_('Notes')}</h3>"""),
            *rows,
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
        advance = kwargs.pop("advance", False)
        super().__init__(*args, **kwargs)
        if advance:
            self.fields['finalize'] = forms.BooleanField(
                required=False,
                initial=False,
                widget=UnfoldBooleanSwitchWidget,
                label=_("Issue as final now"),
                help_text=_(
                    "Assigns the invoice number straight away. VAT on an advance is due "
                    "when it is received, so an advance is usually issued once it is paid."
                ),
            )
        self.helper = ProjectFileInvoiceNotesFormHelper(advance=advance)


class AdvanceDeductionForm(forms.Form):
    """One advance line the invoice deducts, in the wizard's "advances to
    deduct" step. The line and its bounds come from the server's plan
    (``rows``: advance line pk → DeductionRow); a posted line the plan does
    not offer is refused, and the invoice is checked again on creation."""

    advance_line = forms.IntegerField(widget=forms.HiddenInput())
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.00"),
        widget=UnfoldAdminDecimalFieldWidget,
        label=_("Amount to deduct"),
    )

    def __init__(self, *args, rows=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = rows or {}
        # The row shown: the one the step was built with
        self.row = self.rows.get(self.initial.get('advance_line'))
        if self.row is not None and self.row.fixed:
            self.fields['amount'].widget.attrs['readonly'] = True

    def clean(self):
        cleaned_data = super().clean()
        row = self.rows.get(cleaned_data.get('advance_line'))
        if row is None:
            raise forms.ValidationError(_("This advance cannot be deducted on this invoice."))
        if row.fixed:
            cleaned_data['amount'] = row.amount
        amount = cleaned_data.get('amount')
        if amount is not None and not row.minimum <= amount <= row.maximum:
            self.add_error('amount', _(
                "Enter an amount between %(minimum)s and %(maximum)s."
            ) % {'minimum': row.minimum, 'maximum': row.maximum})
        return cleaned_data
