from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.admin import GenericTabularInline

from core.admin.company_admin import CompanyRestrictedAdmin
from invoices.models import AppliedTax
from management.models import WorkEntryCategory, WorkEntryType, WorkEntryPricingStep




class WorkEntryTypeAppliedTaxInline(GenericTabularInline):
    model = AppliedTax
    extra = 0
    tab = True
    autocomplete_fields = ("tax_type",)
    fields = ("tax_type", "selected_percentage", "order")
    verbose_name = _("Applied Tax")
    verbose_name_plural = _("Applied Taxes")

class WorkEntryPricingStepInline(TabularInline):
    model = WorkEntryPricingStep
    extra = 0
    tab = True
    hide_ordering_field = True
    ordering_field = "order"
    ordering = ("order",)
    fields = ["variable_name", "expression", "order"]



@admin.register(WorkEntryCategory)
class WorkEntryCategoryAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "slug", "description"]
    search_fields = ["name", "slug"]
    autocomplete_fields = ["company"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(WorkEntryType)
class WorkEntryTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "category", "unit_label", "project_file_display", "is_active"]
    list_filter = ["is_active", "category"]
    search_fields = ["name", "category__name"]
    autocomplete_fields = ["company", "category", "project_file", "source_content_type", "materials", "components"]
    inlines = [WorkEntryPricingStepInline, WorkEntryTypeAppliedTaxInline]

    fieldsets = (
        (_("Basic"), {
            "fields": (
                "company",
                "category",
                ("name", "unit_label"),
            ),
        }),
        (_("Pricing Engine"), {
            "fields": (
                "applicability_expression",
                "description_template",
            ),
            "description": _(
                "Pricing is computed via the Pricing Steps inline below. "
                "The applicability expression determines when this type should be "
                "automatically suggested for a source object."
            ),
        }),
        (_("Linked Pricing Data"), {
            "fields": (
                "materials",
                "components",
            ),
            "description": _(
                "Only the materials and components selected here will be available "
                "in the expression context as materials.<rule_key> and components.<slug>."
            ),
        }),
        (_("Scope"), {
            "fields": (
                "source_content_type",
                "project_file",
                "is_active",
            ),
        }),
    )

    @admin.display(description=_("Project File"))
    def project_file_display(self, obj):
        if obj.project_file:
            return obj.project_file.reference
        return _("Company-wide")
