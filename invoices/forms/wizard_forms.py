from django import forms
from crispy_forms.helper import FormHelper
from django.utils.translation import gettext_lazy as _
from crispy_forms.layout import Layout, Row, Column, Field, HTML
from django.conf import settings
from invoices.models import PaymentMethod, SalesInvoice, TaxType
from django.contrib import admin
from core.admin.company_access import get_user_companies
from sales.models import Client
from company.models import Company
from unfold.widgets import (
    UnfoldAdminSelectWidget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminDateWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminRelatedFieldWrapperWidget
)
from unfold.fields import UnfoldAdminAutocompleteModelChoiceField

class SalesInvoiceBasicFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(
            HTML(f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                    <h3 class="text-base font-semibold text-gray-800 mb-3">{_('Invoice Details')}</h3>"""),
            Row(
                Column(Field('company'), css_class="col-span-12"),
                css_class="gap-4 mb-4"
            ),
            Row(
                Column(Field('client'), css_class="col-span-12"),
                css_class="gap-4 mb-4"
            ),
            Row(
                Column(Field('date_issued'), css_class="col-span-12 md:col-span-6"),
                Column(Field('due_date'), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            Row(
                Column(Field('payment_method'), css_class="col-span-12 md:col-span-6"),
                Column(Field('currency'), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            HTML("</div>")
        )

class SalesInvoiceBasicForm(forms.ModelForm):
    company = forms.ModelChoiceField(
        queryset=Company.objects.all(),
        widget=UnfoldAdminSelectWidget,
        required=True,
        label=_("Company")
    )
    client = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:client_autocomplete",
        queryset=Client.objects.all(),
        label=_("Select Client"),
        required=True
    )

    class Media:
        extra = "" if settings.DEBUG else ".min"
        js = (
            f"admin/js/vendor/jquery/jquery{extra}.js",
            "admin/js/vendor/select2/select2.full.js",
            "admin/js/jquery.init.js",
            "unfold/js/select2.init.js",
            "admin/js/admin/RelatedObjectLookups.js",
        )
        css = {
            "screen": (
                "admin/css/vendor/select2/select2.css",
                "admin/css/autocomplete.css",
            ),
        }

    class Meta:
        model = SalesInvoice
        # No status: an invoice created here is always a pro forma; it gets
        # its number and goes to the AEAT only through Finalize.
        fields = ['company', 'client', 'date_issued', 'due_date', 'payment_method', 'currency']
        widgets = {
            'date_issued': UnfoldAdminDateWidget,
            'due_date': UnfoldAdminDateWidget,
            'payment_method': UnfoldAdminSelectWidget,
            'currency': UnfoldAdminSelectWidget,
        }

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self.helper = SalesInvoiceBasicFormHelper()

        if self.request and self.request.user:

            companies = get_user_companies(self.request.user).filter(is_active=True)
            self.fields["company"].queryset = companies
            if len(companies) == 1:
                self.fields["company"].initial = companies[0]

            self.fields["client"].queryset = (
                Client.objects.filter(company__in=companies) if self.is_bound else Client.objects.none()
            )

        if self.request and self.fields.get('client'):
            self.fields['client'].widget = UnfoldAdminRelatedFieldWrapperWidget(
                self.fields['client'].widget,
                self.instance._meta.get_field('client').remote_field,
                admin.site,
                can_add_related=True
            )

    def clean(self):
        cleaned = super().clean()
        client = cleaned.get("client")
        company = cleaned.get("company")
        if client and company and client.company_id != company.pk:
            self.add_error("client", _("This client belongs to another company."))
        if client and not cleaned.get("payment_method"):
            cleaned["payment_method"] = client.payment_method or PaymentMethod.TRANSFER
        return cleaned


class WizardItemFormsetHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.template = "invoices/stacked_inline_formset.html"
        self.form_id = "item-formset"
        self.formset_id = "item-formset"
        self.form_add = True
        self.form_tag = False
        self.template_pack = "unfold_crispy"

class WizardItemForm(forms.Form):
    source_id = forms.CharField(
        widget=forms.HiddenInput(),
        required=False,
    )
    description = forms.CharField(
        label=_("Description"), 
        max_length=500, 
        widget=UnfoldAdminTextareaWidget(attrs={'rows': 2})
    )
    quantity = forms.DecimalField(label=_("Qty"), max_digits=10, decimal_places=3, initial=1, widget=UnfoldAdminDecimalFieldWidget)
    unit_price = forms.DecimalField(label=_("Unit Price"), max_digits=12, decimal_places=2, widget=UnfoldAdminDecimalFieldWidget)
    discount_percentage = forms.DecimalField(label=_("Discount (%)"), max_digits=5, decimal_places=2, initial=0, widget=UnfoldAdminDecimalFieldWidget, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field("source_id"),
            Row(
                Column(Field("description"), css_class="col-span-12"),
                css_class="mb-4"
            ),
            Row(
                Column(Field("quantity"), css_class="col-span-12 md:col-span-4"),
                Column(Field("unit_price"), css_class="col-span-12 md:col-span-4"),
                Column(Field("discount_percentage"), css_class="col-span-12 md:col-span-4"),
                css_class="gap-4"
            )
        )

    def clean(self):
        cleaned_data = super().clean()
        # The source line's pinned total (offered as an initial value, never
        # posted) holds while its quantity, price and discount are untouched
        pinned = self.initial.get("override_line_total")
        edited = {"quantity", "unit_price", "discount_percentage"} & set(self.changed_data)
        cleaned_data["override_line_total"] = None if edited else pinned
        return cleaned_data


class WizardTaxFormsetHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.template = "invoices/stacked_inline_formset.html"
        self.form_id = "tax-formset"
        self.formset_id = "tax-formset"
        self.form_add = True
        self.form_tag = False
        self.template_pack = "unfold_crispy"

class WizardTaxForm(forms.Form):
    tax_type = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:invoices_taxtype_autocomplete",
        queryset=TaxType.objects.filter(is_active=True),
        label=_("Tax Type"),
        required=True
    )
    selected_percentage = forms.DecimalField(label=_("Percentage"), max_digits=5, decimal_places=2, initial=21, widget=UnfoldAdminDecimalFieldWidget)
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column(Field("tax_type"), css_class="col-6"),
                Column(Field("selected_percentage"), css_class="col-6"),
            )
        )

class SalesInvoiceNotesFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(
            HTML(f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                    <h3 class="text-base font-semibold text-gray-800 mb-3">{_('Notes')}</h3>"""),
            Row(
                Column(Field('notes'), css_class="col-span-12"),
            ),
            HTML("</div>")
        )

class SalesInvoiceNotesForm(forms.ModelForm):
    class Meta:
        model = SalesInvoice
        fields = ['notes']
        widgets = {
            'notes': UnfoldAdminTextareaWidget,
        }

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self.helper = SalesInvoiceNotesFormHelper()
