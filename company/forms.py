from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminSplitDateTimeWidget

from general.onboarding.forms import UnfoldFormMixin

from .models import EmployeeDocument, MachineDocument, CompanyDocument, Employee

class BaseDocumentForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and getattr(self.instance, "does_not_expire", False):
            if "expiry_date" in self.fields:
                self.fields["expiry_date"].widget = forms.HiddenInput()
                self.fields["expiry_date"].label = ""

class EmployeeDocumentForm(BaseDocumentForm):
    class Meta:
        model = EmployeeDocument
        fields = "__all__"

class MachineDocumentForm(BaseDocumentForm):
    class Meta:
        model = MachineDocument
        fields = "__all__"

class CompanyDocumentForm(BaseDocumentForm):
    class Meta:
        model = CompanyDocument
        fields = "__all__"


class DriverTrackingFilterForm(forms.Form):
    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        label=_("Driver"),
        empty_label=_("All"),
        widget=UnfoldAdminSelectWidget,
    )
    start = forms.SplitDateTimeField(
        required=False,
        label=_("From"),
        widget=UnfoldAdminSplitDateTimeWidget,
    )
    end = forms.SplitDateTimeField(
        required=False,
        label=_("To"),
        widget=UnfoldAdminSplitDateTimeWidget,
    )

    def __init__(self, *args, drivers=None, **kwargs):
        super().__init__(*args, **kwargs)
        if drivers is not None:
            self.fields["employee"].queryset = drivers

    class Media:
        extra = "" if settings.DEBUG else ".min"
        js = [
            f"admin/js/vendor/jquery/jquery{extra}.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]


class NewCompanyForm(UnfoldFormMixin, forms.Form):
    """What staff give to create a company's shell and invite its first admin."""

    name = forms.CharField(label=_("Commercial Name"), max_length=100)
    legal_name = forms.CharField(label=_("Legal Name"), max_length=100)
    tax_identifier = forms.CharField(label=_("Tax Identifier"), max_length=20)
    constituted_country = forms.ChoiceField(label=_("Country"), initial="ES")
    logo = forms.ImageField(label=_("Logo"))
    chart_template = forms.ModelChoiceField(label=_("Chart of accounts"), queryset=None)
    modules = forms.MultipleChoiceField(
        label=_("Modules"), required=False, widget=forms.CheckboxSelectMultiple,
        help_text=_("Optional areas the company uses; decides which setup steps it is shown."),
    )
    admin_email = forms.EmailField(label=_("Admin email"))
    admin_first_name = forms.CharField(label=_("Admin first name"), max_length=50)
    admin_last_name = forms.CharField(label=_("Admin last name"), max_length=50)

    def __init__(self, *args, **kwargs):
        from django_countries import countries

        from accounting.models import ChartOfAccountsTemplate
        from company.models import CompanyModule

        super().__init__(*args, **kwargs)
        self.fields["constituted_country"].choices = list(countries)
        templates = ChartOfAccountsTemplate.objects.order_by("-is_default", "name")
        self.fields["chart_template"].queryset = templates
        self.fields["chart_template"].initial = templates.filter(is_default=True).first()
        self.fields["modules"].choices = CompanyModule.choices
