from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import ProductTemplate, ProductParameter, BOMRule, ProductComputedVariable,ProductPricingRuleAssignment, ProductTemplateComponentGroup, ProductComputedTemplate
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
    BooleanRadioFilter,
    ChoicesDropdownFilter,
)
from unfold.admin import TabularInline,StackedInline,GenericTabularInline
from django.db import models
from unfold.widgets import UnfoldAdminTextareaWidget, UnfoldAdminSelectWidget
from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField
from django.utils.translation import gettext_lazy as _
from django.shortcuts import redirect
from django.urls import path, reverse
from django.http import HttpResponse
from configurator.views.product_template.product_template_resources_view import ProductTemplateResourcesView
from configurator.views.product_template.import_planning_view import ImportPlanningView
from production.views.product_stage_report_view import ProductStageReportView
from configurator.resources.planning_export import export_planning_xlsx
from invoices.models import AppliedTax
from unfold.decorators import action
from django.utils.html import format_html
from django import forms
from core.admin.company_admin import CompanyRestrictedAdmin
from materials.models import Component


class AppliedTaxInline(GenericTabularInline):
    ordering_field = "order"
    hide_ordering_field=True
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    fields = ("tax_type", "selected_percentage", "order")
    verbose_name = _("Applied tax")
    verbose_name_plural = _("Applied taxes")
    tab = True



class ProductComputedVariableInline(TabularInline):
    model = ProductComputedVariable
    extra = 0
    fields = ("name", "expression")
    verbose_name = _("Computed Variable")
    verbose_name_plural = _("Computed Variables")
    tab = True


class ProductComputedTemplateInline(StackedInline):
    model = ProductComputedTemplate
    extra = 0
    fields = ("name", "condition_expression", "template", "order")
    verbose_name = _("Computed Template")
    verbose_name_plural = _("Computed Templates")
    tab = True
    ordering_field = "order"
    hide_ordering_field = True
    
    formfield_overrides = {
        models.TextField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 4})},
    }


class ProductParameterInline(TabularInline):
    model = ProductParameter
    extra = 0
    tab=True
    hide_ordering_field = True
    ordering_field = "order"
    autocomplete_fields = ["product"]
    
    formfield_overrides = {
        models.JSONField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }

    def get_formset(self, request, obj=None, **kwargs):
        """
        obj is the parent ProductTemplate instance
        """
        formset = super().get_formset(request, obj, **kwargs)

        if obj and obj.parameter_group_options:
            choices = [("", "---------")] + [
            (opt, opt) for opt in obj.parameter_group_options
        ]

            formset.form.base_fields["parameter_group"] = forms.ChoiceField(
                choices=choices,
                required=False,
                widget=UnfoldAdminSelectWidget
            )

        return formset

class BOMRuleInline(StackedInline):
    model = BOMRule
    extra = 0
    fields = ("component", ("quantity_expression", "length_expression", "note_expression"), ("production_stages", "description"))
    tab=True
    autocomplete_fields = ["component"]

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)

        if obj and obj.production_stages:
            formset.form.base_fields["production_stages"].widget = ArrayWidget(
                choices=[("", "---------")] + [(s, s) for s in obj.production_stages]
            )

        return formset

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("component","product")


class ProductPricingRuleAssignmentInline(TabularInline):
    model = ProductPricingRuleAssignment
    extra = 0
    fields = ("rule",)
    tab=True
    ordering_field = "order"
    hide_ordering_field=True
    autocomplete_fields = ["rule"]

    formfield_overrides = {
        models.TextField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }


class ProductTemplateComponentGroupInline(StackedInline):
    verbose_name = _("Stage component group")
    verbose_name_plural = _("Stage component groups")
    model = ProductTemplateComponentGroup
    extra = 0
    tab = True

    fields = ("stage", "components",)
    filter_horizontal = ("components",)

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)

        if obj and obj.production_stages:
            choices = [("", "---------")] + [
            (s, s) for s in obj.production_stages
        ]

            formset.form.base_fields["stage"] = forms.ChoiceField(
                choices=choices,
                required=False,
                widget=UnfoldAdminSelectWidget
            )

        return formset

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "components":
            object_id = request.resolver_match.kwargs.get("object_id")
            if object_id:
                component_ids = (
                    BOMRule.objects
                    .filter(product_id=object_id)
                    .values_list("component_id", flat=True)
                    .distinct()
                )
                kwargs["queryset"] = Component.objects.filter(id__in=component_ids).order_by("name")
            else:
                kwargs["queryset"] = Component.objects.none()

        return super().formfield_for_manytomany(db_field, request, **kwargs)


@admin.register(ProductTemplate)
class ProductTemplateAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ("id", "name", "flow_kind", "company", "show_in_configurator", "planning_configured_badge", "planning_configure_link")
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("flow_kind", ChoicesDropdownFilter),
        ("show_in_configurator", BooleanRadioFilter),
        ("production_planning_configured", BooleanRadioFilter),
        ("exclude_from_invoicing", BooleanRadioFilter),
    )
    list_filter_submit = True
    search_fields = ("name", "description")
    actions_row = ["duplicate_product", "stage_report_action"]
    actions = ["export_planning_config"]
    list_select_related = ["company"]
    autocomplete_fields = ["company"]

    inlines = [
        ProductParameterInline,
        ProductComputedVariableInline,
        ProductComputedTemplateInline,
        BOMRuleInline,
        ProductPricingRuleAssignmentInline,
        AppliedTaxInline,
        ProductTemplateComponentGroupInline,
    ]

    fieldsets = (
        (_("Product Template"), {
            "fields": (("name", "image"), "description", ("production_stages", "parameter_group_options"), ("flow_kind", "unit_price_variable"), ("allow_production_without_accepted_quote", "allow_parameter_values_edit_in_any_status"), "exclude_from_invoicing"),
        })
        ,(_("Ownership"), {
            "fields": ("company",),
        })
    )

    @admin.display(description=_("Planning configured"), boolean=True)
    def planning_configured_badge(self, obj: ProductTemplate):
        return bool(obj.production_planning_configured)

    @admin.display(description=_("Planning"))
    def planning_configure_link(self, obj: ProductTemplate):
        url = reverse("admin:configurator_producttemplate_planning", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("Configure"))

    formfield_overrides = {
        models.TextField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    @action(
        description=_("Clone"),
        url_path="producttemplate-admin-clone",
    )
    def duplicate_product(self, request, object_id: int):
        """
        Clone this product template and redirect to the new copy.
        """
        obj = ProductTemplate.objects.get(pk=object_id)
        new_product = obj.clone()

        self.message_user(
            request,
            _("Product template '%(name)s' was successfully duplicated.")
            % {"name": obj.name},
        )

        return redirect(
            reverse(
                "admin:configurator_producttemplate_change",
                args=[new_product.pk],
            )
        )
    
    @action(
        description=_("Stage report"),
        url_path="producttemplate-admin-stage-report",
        attrs={"target": "_blank"},
    )
    def stage_report_action(self, request, object_id: int):
        url = reverse("admin:configurator_producttemplate_stage_report")
        return redirect(f"{url}?product={object_id}")

    @action(
        description=_("Export planning config"),
    )
    def export_planning_config(self, request, queryset):
        xlsx_bytes = export_planning_xlsx(queryset)
        response = HttpResponse(
            xlsx_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="planning_config.xlsx"'
        return response

    @admin.display(description=_("Import planning"))
    def import_planning_link(self, obj=None):
        url = reverse("admin:configurator_producttemplate_import_planning")
        return format_html(
            '<a class="button" href="{}">{}</a>',
            url,
            _("Import Planning"),
        )

    def get_urls(self):
        urls = super().get_urls()
        planning_view = self.admin_site.admin_view(
            ProductTemplateResourcesView.as_view(model_admin=self)
        )
        stage_report_view = self.admin_site.admin_view(
            ProductStageReportView.as_view(model_admin=self)
        )
        import_planning_view = self.admin_site.admin_view(
            ImportPlanningView.as_view(model_admin=self)
        )

        return [
            path("<int:pk>/planning/", planning_view, name="configurator_producttemplate_planning"),
            path("stage-report/", stage_report_view, name="configurator_producttemplate_stage_report"),
            path("import-planning/", import_planning_view, name="configurator_producttemplate_import_planning"),
        ] + urls