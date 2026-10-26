from django import forms
from django.core.validators import FileExtensionValidator
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminFileFieldWidget, UnfoldAdminSelectWidget

from company.models import Company, Employee


class CompanyChoiceField(forms.ModelChoiceField):
    """Company select labelled with the tax id, so shared NIFs stay distinct."""

    def label_from_instance(self, obj):
        tax_id = (obj.tax_identifier or "").strip()
        if tax_id:
            return f"{obj.name} ({tax_id})"
        return obj.name


class A3PayrollImportForm(forms.Form):
    company = CompanyChoiceField(
        queryset=Company.objects.none(),
        required=False,
        label=_("Import into company"),
        empty_label=_("Detect from the file"),
        widget=UnfoldAdminSelectWidget(),
        help_text=_(
            "Choose the company when several share the file NIF. "
            "Leave empty to detect it from the file."
        ),
    )
    file = forms.FileField(
        label=_("A3 Payroll Excel"),
        validators=[FileExtensionValidator(allowed_extensions=["xlsx", "xls"])],
        widget=UnfoldAdminFileFieldWidget(attrs={"accept": ".xlsx,.xls"}),
        help_text=_(
            "Upload the A3 payroll export (.xlsx or .xls) for one company. "
            "Both layouts (pivoted and tabular with DNI) are supported. "
            "The company and period are read from the file header."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = Company.objects.order_by("name")


class ImportCompanySelectForm(forms.Form):
    """Pick the company for an import whose file NIF did not resolve
    (missing, unknown, or shared by more than one company)."""

    company = CompanyChoiceField(
        queryset=Company.objects.none(),
        label=_("Import into company"),
        widget=UnfoldAdminSelectWidget(),
    )

    def __init__(self, *args, candidate_ids=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Company.objects.all()
        if candidate_ids:
            qs = qs.filter(pk__in=candidate_ids)
        self.fields["company"].queryset = qs.order_by("name")


class PendingRowResolveForm(forms.Form):
    """Assign an unmatched import row to an existing employee."""

    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        label=_("Assign to employee"),
        widget=UnfoldAdminSelectWidget(),
    )
    set_a3_code = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Remember A3 code on the employee profile (auto-match next time)"),
    )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Employee.objects.all()
        if company is not None:
            qs = qs.filter(company=company)
        self.fields["employee"].queryset = qs.order_by("last_name", "first_name")
