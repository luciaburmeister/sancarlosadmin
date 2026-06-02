from django import forms
from django.db import models
from django.contrib import admin
from django.urls import path, reverse
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action
from unfold.widgets import (
    UnfoldAdminTextareaWidget,
    UnfoldAdminSelect2Widget,
    UnfoldAdminTextInputWidget,
)

from core.admin.company_admin import CompanyRestrictedAdmin
from configurator.commission.models import (
    AgentCommissionRule,
    CommissionRuleTemplate,
    CommissionRuleType,
    QuoteCommission,
    format_rule_snapshot,
)
from configurator.commission.services import CommissionService


class AgentCommissionRuleForm(forms.ModelForm):
    expression_name = forms.CharField(
        required=False,
        label=_("Rule name"),
        widget=UnfoldAdminTextInputWidget(),
        help_text=_(
            "Optional name."
            "If left blank, a name will be generated automatically."
        ),
    )

    expression = forms.CharField(
        required=False,
        label=_("Expression"),
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 4}),
        help_text=_(
            "Variables: subtotal, total, quantity.\n"
            "Examples: subtotal * 0.05 — 5% of subtotal | "
            "200 — flat €200 | "
            "subtotal * 0.05 if subtotal > 5000 else 0 — 5% only above €5,000"
        ),
    )

    class Meta:
        model = AgentCommissionRule
        fields = "__all__"
        widgets = {
            "rule_type": UnfoldAdminSelect2Widget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        instance = kwargs.get("instance")
        if instance and instance.pk and instance.rule_type == CommissionRuleType.CUSTOM:
            if instance.custom_rule:
                self.fields["expression_name"].initial = instance.custom_rule.name
                self.fields["expression"].initial = instance.custom_rule.expression

    def clean(self):
        cleaned_data = super().clean()
        rule_type = cleaned_data.get("rule_type")
        value = cleaned_data.get("value")
        expression = (cleaned_data.get("expression") or "").strip()

        if rule_type == CommissionRuleType.CUSTOM:
            if not expression:
                self.add_error(
                    "expression",
                    _("An expression is required when rule type is Custom."),
                )
            cleaned_data["value"] = None

        elif rule_type in (CommissionRuleType.PERCENTAGE, CommissionRuleType.FIXED):
            if value is None:
                self.add_error(
                    "value",
                    _("Value is required for Percentage and Fixed rules."),
                )
            cleaned_data["custom_rule"] = None
            cleaned_data["expression"] = ""
            cleaned_data["expression_name"] = ""

        return cleaned_data



class AgentCommissionRuleInline(TabularInline):
    model = AgentCommissionRule
    extra = 1
    tab = True
    verbose_name = _("Agent commission rule")
    verbose_name_plural = _("Agent commission rules")
    autocomplete_fields = ["employee", "custom_rule"]
    fields = ("employee", "rule_type", "value", "custom_rule", "is_active")



class QuoteCommissionInline(TabularInline):
    model = QuoteCommission
    extra = 0
    tab = True
    verbose_name = _("Commission")
    verbose_name_plural = _("Commissions")
    autocomplete_fields = ["employee"]

    readonly_fields = (
        "configuration",
        "product_template_display",
        "rule_type_snapshot",
        "rule_value_snapshot",
        "commission_amount",
    )

    fields = (
        "configuration",
        "product_template_display",
        "employee",
        "rule_type_snapshot",
        "rule_value_snapshot",
        "commission_amount",
    )

    def product_template_display(self, obj):
        if obj.configuration_id:
            return obj.configuration.product.name
        return "-"
    product_template_display.short_description = _("Product template")

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False



@admin.register(CommissionRuleTemplate)
class CommissionRuleTemplateAdmin(ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "expression")

    fieldsets = (
        (_("Rule"), {
            "fields": (
                "name",
                "description",
                "expression",
                "is_active",
            ),
        }),
    )

    formfield_overrides = {
        models.TextField: {
            "widget": UnfoldAdminTextareaWidget(attrs={"rows": 4})
        },
    }



@admin.register(AgentCommissionRule)
class AgentCommissionRuleAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = AgentCommissionRuleForm
    change_form_template = "admin/configurator/agentcommissionrule/change_form.html"

    list_display = (
        "employee",
        "product_template",
        "rule_type",
        "value_display",
        "is_active",
        "company",
    )
    list_filter = ("rule_type", "is_active", "company")
    search_fields = (
        "employee__first_name",
        "employee__last_name",
        "product_template__name",
    )
    autocomplete_fields = ["employee", "product_template", "company"]
    list_select_related = ["employee", "product_template", "company"]

    actions_list = ["bulk_create_action"]

    fieldsets = (
        (_("Who gets the commission"), {
            "fields": (
                ("employee", "product_template"),
                ("company", "is_active"),
            ),
        }),
        (_("Commission rule"), {
            "fields": (
                ("rule_type", "value"),
                "expression_name",
                "expression",
            ),
        }),
    )

    def value_display(self, obj):
        return obj.format_value()
    value_display.short_description = _("Value / Rule")

    def save_model(self, request, obj, form, change):
        rule_type = form.cleaned_data.get("rule_type")
        expression = (form.cleaned_data.get("expression") or "").strip()
        expression_name = (form.cleaned_data.get("expression_name") or "").strip()

        if rule_type == CommissionRuleType.CUSTOM and expression:
            # Check if this exact expression already exists in a DIFFERENT template
            existing = CommissionRuleTemplate.objects.filter(
                expression=expression
            ).exclude(
                pk=obj.custom_rule_id if obj.custom_rule_id else None
            ).first()

            if existing:
                self.message_user(
                    request,
                    _(
                        f"This expression already exists as template "
                        f"'{existing.name}'. "
                        f"The rule was not saved. "
                        f"Please use a different expression or select "
                        f"the existing template."
                    ),
                    level="error",
                )
                return  

            if obj.custom_rule_id:
                template = obj.custom_rule
                if not expression_name:
                    expression_name = template.name 
                template.name = expression_name
                template.expression = expression
                template.save()
            else:
                template = CommissionRuleTemplate.objects.filter(
                    expression=expression
                ).first()

                if not template:
                    # Create a new template
                    if not expression_name:
                        employee_str = str(obj.employee) if obj.employee_id else "?"
                        product_str = str(obj.product_template) if obj.product_template_id else "?"
                        expression_name = f"Custom: {employee_str} — {product_str}"
                    template = CommissionRuleTemplate.objects.create(
                        name=expression_name,
                        expression=expression,
                        is_active=True,
                    )

            obj.custom_rule = template
            obj.value = None

        super().save_model(request, obj, form, change)

    @action(description=_("Bulk create rules"), icon="add")
    def bulk_create_action(self, request):
        return redirect(
            reverse("admin:configurator_agentcommissionrule_bulk_create")
        )

    def get_urls(self):
        from configurator.commission.views import (
            BulkCommissionWizardView,
            BulkCommissionConfirmView,
        )
        from configurator.commission.autocomplete_views import (
            EmployeeAutocompleteView,
            ProductTemplateAutocompleteView,
        )
        custom_urls = [
            path(
                "bulk-create/",
                self.admin_site.admin_view(
                    BulkCommissionWizardView.as_view(model_admin=self)
                ),
                name="configurator_agentcommissionrule_bulk_create",
            ),
            path(
                "bulk-confirm/",
                self.admin_site.admin_view(
                    BulkCommissionConfirmView.as_view(model_admin=self)
                ),
                name="configurator_agentcommissionrule_bulk_confirm",
            ),
            path(
                "autocomplete/employee/",
                self.admin_site.admin_view(EmployeeAutocompleteView.as_view()),
                name="commission_employee_autocomplete",
            ),
            path(
                "autocomplete/producttemplate/",
                self.admin_site.admin_view(ProductTemplateAutocompleteView.as_view()),
                name="commission_producttemplate_autocomplete",
            ),
        ]
        return custom_urls + super().get_urls()



@admin.register(QuoteCommission)
class QuoteCommissionAdmin(ModelAdmin):
    list_display = (
        "configuration",
        "employee",
        "rule_type_snapshot",
        "rule_value_display",
        "commission_amount",
        "calculated_at",
    )
    list_filter = ("rule_type_snapshot",)
    search_fields = (
        "employee__first_name",
        "employee__last_name",
    )
    autocomplete_fields = ["employee"]
    readonly_fields = (
        "commission_amount",
        "rule",
        "rule_type_snapshot",
        "rule_value_snapshot",
        "calculated_at",
    )
    list_select_related = ["employee", "configuration", "rule"]

    def rule_value_display(self, obj):
        return format_rule_snapshot(obj.rule_type_snapshot, obj.rule_value_snapshot)
    rule_value_display.short_description = _("Value")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        CommissionService.recalculate(obj.configuration.quote)