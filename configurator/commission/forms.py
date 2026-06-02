from django import forms
from django.utils.translation import gettext_lazy as _
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column, HTML
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminSelect2Widget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminTextareaWidget,
)
from company.models import Employee
from configurator.models import ProductTemplate
from configurator.commission.models import CommissionRuleType, CommissionRuleTemplate
from unfold.fields import UnfoldAdminMultipleAutocompleteModelChoiceField


EXPRESSION_HELP = _(
            "Variables: subtotal, total, quantity.\n"
            "Examples: subtotal * 0.05 — 5% of subtotal | "
            "200 — flat €200 | "
            "subtotal * 0.05 if subtotal > 5000 else 0 — 5% only above €5,000"
        )


class BulkCommissionStep1Form(forms.Form):
    """
    Step 1: Select employees and product templates using autocomplete multi-select.
    """

    employees = UnfoldAdminMultipleAutocompleteModelChoiceField(
        queryset=Employee.objects.all(),
        label=_("Employees"),
        url_path="admin:commission_employee_autocomplete",
        required=True,
    )

    product_templates = UnfoldAdminMultipleAutocompleteModelChoiceField(
        queryset=ProductTemplate.objects.all(),
        label=_("Product templates"),
        url_path="admin:commission_producttemplate_autocomplete",
        required=True,
    )

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)

        company = None
        if request and request.user.is_authenticated:
            from company.models import CompanyUser
            company_user = CompanyUser.objects.filter(
                user=request.user
            ).select_related("company").first()
            company = company_user.company if company_user else None

        if company:
            self.fields["employees"].queryset = (
                Employee.objects.filter(company=company, is_active=True)
                .order_by("last_name", "first_name")
            )
            self.fields["product_templates"].queryset = (
                ProductTemplate.objects.filter(company=company)
                .order_by("name")
            )
        else:
            self.fields["employees"].queryset = (
                Employee.objects.filter(is_active=True)
                .order_by("last_name", "first_name")
            )
            self.fields["product_templates"].queryset = (
                ProductTemplate.objects.all().order_by("name")
            )

        self.helper = BulkCommissionStep1FormHelper()


class BulkCommissionStep1FormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(
            HTML(
                """<div class="col-span-12 mb-6 rounded-lg border border-gray-200 """
                """bg-white dark:bg-gray-900 p-4">"""
            ),
            HTML(
                f"""<h3 class="text-base font-semibold text-gray-800 dark:text-gray-100 mb-1">"""
                f"""{_('Select employees and product templates')}</h3>"""
                f"""<p class="text-xs text-gray-500 mb-4">"""
                f"""{_('Search and select all employees and product templates that will receive these commission rules.')}</p>"""
            ),
            Row(
                Column(Field("employees", size=10), css_class="col-span-12"),
                css_class="mb-4",
            ),
            Row(
                Column(Field("product_templates", size=10), css_class="col-span-12"),
                css_class="mb-4",
            ),
            HTML("</div>"),
        )


class BulkCommissionStep2Form(forms.Form):
    """
    Step 2: Define the commission rule.

    - Percentage / Fixed: show value (number)
    - Custom: show expression_name (optional) + expression (textarea)

    The value/expression fields are toggled by JS in the wizard template.
    """

    rule_type = forms.ChoiceField(
        choices=CommissionRuleType.choices,
        label=_("Rule type"),
        initial=CommissionRuleType.PERCENTAGE,
        widget=UnfoldAdminSelect2Widget(),
    )

    value = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        label=_("Value"),
        widget=UnfoldAdminDecimalFieldWidget(),
    )

    expression_name = forms.CharField(
        required=False,
        label=_("Rule name"),
        widget=UnfoldAdminTextInputWidget(),
    )

    expression = forms.CharField(
        required=False,
        label=_("Expression"),
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 4}),
        help_text=EXPRESSION_HELP,
    )

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = BulkCommissionStep2FormHelper()

    def clean(self):
        cleaned_data = super().clean()
        rule_type = cleaned_data.get("rule_type")
        value = cleaned_data.get("value")
        expression = (cleaned_data.get("expression") or "").strip()

        if rule_type in (CommissionRuleType.PERCENTAGE, CommissionRuleType.FIXED):
            if value is None:
                self.add_error(
                    "value",
                    _("Value is required for percentage and fixed rules."),
                )
            cleaned_data["expression"] = ""
            cleaned_data["expression_name"] = ""

        elif rule_type == CommissionRuleType.CUSTOM:
            if not expression:
                self.add_error(
                    "expression",
                    _("An expression is required for custom rules."),
                )
            cleaned_data["value"] = None

        return cleaned_data


class BulkCommissionStep2FormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(
            HTML(
                """<div class="col-span-12 mb-6 rounded-lg border border-gray-200 """
                """bg-white dark:bg-gray-900 p-4">"""
                f"""<h3 class="text-base font-semibold text-gray-800 dark:text-gray-100 mb-1">"""
                f"""{_('Commission Rule')}</h3>"""
                f"""<p class="text-xs text-gray-500 mb-3">"""
                f"""{_('This rule will be applied to all selected employee × product template combinations.')}</p>"""
            ),
            Row(
                Column(Field("rule_type"), css_class="col-span-12 md:col-span-6"),
                Column(Field("value"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-2",
            ),
            Row(
                Column(Field("expression_name"), css_class="col-span-12"),
                css_class="mb-2",
            ),
            Row(
                Column(Field("expression"), css_class="col-span-12"),
                css_class="mb-4",
            ),
            HTML("</div>"),
        )