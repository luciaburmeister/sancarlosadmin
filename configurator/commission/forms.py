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
from sales.models import CommissionistAgent
from configurator.commission.models import CommissionRuleType, CommissionRuleTemplate
from unfold.fields import UnfoldAdminMultipleAutocompleteModelChoiceField


EXPRESSION_HELP = _(
            "Variables: subtotal, discounted_subtotal, total, quantity.\n"
            "Examples: subtotal * 0.05 — 5% of subtotal | "
            "200 — flat €200 | "
            "discounted_subtotal * 0.05 if discounted_subtotal > 5000 else 0 — 5% only above €5,000"
        )


class BulkCommissionStep1Form(forms.Form):
    """
    Step 1: Select agents using autocomplete multi-select.
    """

    agents = UnfoldAdminMultipleAutocompleteModelChoiceField(
        queryset=CommissionistAgent.objects.all(),
        label=_("Agents"),
        url_path="admin:commission_agent_autocomplete",
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
            self.fields["agents"].queryset = (
                CommissionistAgent.objects.filter(company=company, is_active=True)
                .order_by("name")
            )
        else:
            self.fields["agents"].queryset = (
                CommissionistAgent.objects.filter(is_active=True)
                .order_by("name")
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
                """\u003cdiv class="col-span-12 mb-6 rounded-lg border border-gray-200 """
                """bg-white dark:bg-gray-900 p-4"\u003e"""
            ),
            HTML(
                f"""\u003ch3 class="text-base font-semibold text-gray-800 dark:text-gray-100 mb-1"\u003e"""
                f"""{_('Select agents')}\u003c/h3\u003e"""
                f"""\u003cp class="text-xs text-gray-500 mb-4"\u003e"""
                f"""{_('Search and select all agents that will receive these commission rules.')}\u003c/p\u003e"""
            ),
            Row(
                Column(Field("agents", size=10), css_class="col-span-12"),
                css_class="mb-4",
            ),
            HTML("\u003c/div\u003e"),
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
                """\u003cdiv class="col-span-12 mb-6 rounded-lg border border-gray-200 """
                """bg-white dark:bg-gray-900 p-4"\u003e"""
                f"""\u003ch3 class="text-base font-semibold text-gray-800 dark:text-gray-100 mb-1"\u003e"""
                f"""{_('Commission Rule')}\u003c/h3\u003e"""
                f"""\u003cp class="text-xs text-gray-500 mb-3"\u003e"""
                f"""{_('This rule will be applied to all selected agents.')}\u003c/p\u003e"""
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
            HTML("\u003c/div\u003e"),
        )