from django import forms
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.admin import GenericTabularInline

from core.admin.company_admin import CompanyRestrictedAdmin
from invoices.models import AppliedTax
from management.models import WorkEntryCategory, WorkEntryDirection, WorkEntryType, WorkEntryPricingStep




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


class WorkEntryTypeAdminForm(forms.ModelForm):
    class Meta:
        model = WorkEntryType
        exclude = ("project_file",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        if (
            instance.pk
            and not self.is_bound
            and "project_files" in self.fields
            and instance.project_file_id
        ):
            selected = list(self.initial.get("project_files") or [])
            selected_ids = {getattr(pf, "pk", pf) for pf in selected}
            if instance.project_file_id not in selected_ids:
                selected.append(instance.project_file)
            self.initial["project_files"] = selected

    def clean(self):
        cleaned = super().clean()
        if self.errors or "project_files" not in cleaned:
            return cleaned
        if cleaned.get("company") is not None:
            self.instance.company = cleaned["company"]
        if "category" in cleaned:
            self.instance.category = cleaned.get("category")
        if cleaned.get("direction"):
            self.instance.direction = cleaned["direction"]
        self.instance.validate_project_scope(cleaned.get("project_files") or [])
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.apply_legacy_project_file(self.cleaned_data.get("project_files") or [])
        if commit:
            instance.save()
            self.save_m2m()
        return instance


@admin.register(WorkEntryType)
class WorkEntryTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = WorkEntryTypeAdminForm
    list_display = ["name", "direction", "category", "unit_label", "project_file_display", "is_active"]
    list_filter = ["is_active", "direction", "category"]
    search_fields = ["name", "category__name"]
    autocomplete_fields = [
        "company",
        "category",
        "project_files",
        "source_content_type",
        "materials",
        "components",
    ]
    inlines = [WorkEntryPricingStepInline, WorkEntryTypeAppliedTaxInline]

    fieldsets = (
        (_("Basic"), {
            "fields": (
                "company",
                ("direction", "category"),
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
                "project_files",
                "is_active",
            ),
        }),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("project_file", "category")
            .prefetch_related("project_files")
        )

    def get_inlines(self, request, obj=None):
        # Cost entries carry net amounts only, so cost types have no taxes.
        if obj is not None and obj.direction == WorkEntryDirection.COST:
            return [WorkEntryPricingStepInline]
        return super().get_inlines(request, obj)

    @admin.display(description=_("Project Files"))
    def project_file_display(self, obj):
        labels = [
            project_file.reference or str(project_file)
            for project_file in obj.scoped_project_files()
        ]
        if labels:
            return ", ".join(labels)
        return _("Company-wide")
