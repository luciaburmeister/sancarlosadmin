from django.contrib import admin
from unfold.admin import ModelAdmin
from materials.models import (
    Category, MaterialFamily, Material, Provider,
    Component, ComponentPriceRule, PriceSheet, PriceSheetItem,
)
from unfold.admin import TabularInline
from django_json_widget.widgets import JSONEditorWidget
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter
)
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import (
    ImportForm,
    SelectableFieldsExportForm,
)
from materials.resources import MaterialResource, PriceSheetItemResource

class ComponentPriceRuleInline(TabularInline):
    model = ComponentPriceRule
    extra = 0
    hide_ordering_field = True
    ordering_field = "priority"
    tab=True

    fields = (
        "condition",
        "unit_price_expression",
        "unit_price",
        "is_fallback",
    )

    verbose_name = _("Component price rule")
    verbose_name_plural = _("Component price rules")


@admin.register(Component)
class ComponentAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = (
        "name",
        "code",
        "component_type",
        "company"
    )

    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        "materials",
        "component_type",
    )
    list_filter_submit = True

    list_select_related = ["company"]

    search_fields = (
        "name",
        "code",
        "materials__name",
        "materials__reference_code",
    )

    autocomplete_fields = ("materials","company")

    inlines = [ComponentPriceRuleInline]  # 👈 HERE

    formfield_overrides = {
        models.JSONField:{
            'widget': JSONEditorWidget,
        },
         ArrayField: {
            "widget": ArrayWidget,
        }
        }

    fieldsets = [
        (
            None,
            {
                "fields": (
                    "materials",
                    ("name",
                    "code"),
                    "component_type", 
                    ("unit_price_expression","cost_per_unit"),
                    ("company","production_stages")
                )
            },
        ),
        (
            "Metadata",
            {
                "fields": ("metadata",),
            },
        ),
    ]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj and obj.company and obj.company.production_stages:
            form.base_fields["production_stages"].widget = ArrayWidget(
                choices=[("", "---------")] + [(s, s) for s in obj.company.production_stages]
            )
        return form


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(MaterialFamily)
class MaterialFamilyAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "company"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]

@admin.register(Material)
class MaterialAdmin(CompanyRestrictedAdmin, ModelAdmin, ImportExportModelAdmin):
    resource_classes = [MaterialResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm

    list_display = [
        "reference_code",
        "name",
        "category",
        "family",
        "cost_per_unit",
        "unit_type",
        "company",
    ]
    list_filter = ["category", "provider"]
    search_fields = ["reference_code", "name"]
    ordering = ["reference_code"]
    list_filter = [
        ("provider", AutocompleteSelectMultipleFilter),
        ("category", AutocompleteSelectMultipleFilter),
        ("family", AutocompleteSelectMultipleFilter),
    ]
    list_filter_submit = True
    autocomplete_fields = ["provider", "company", "category"]
    list_select_related = ["company", "category"]

    fieldsets = (
        ("Product Details", {
            "fields": (
                ("name"),
                ("reference_code", "rule_key"),
                ("category", "family"),
                ("cost_per_unit", "unit_type"),
                ("provider", "company"),
            )
        }),
    )

    def has_import_permission(self, request):
        return request.user.has_perm("materials.can_import_materials")


@admin.register(Provider)
class ProviderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "contact", "phone", "email", "tax_identifier", "account", "company")
    search_fields = ("name", "contact", "phone", "email", "tax_identifier", "associated_emails")
    list_select_related = ["company", "account"]
    autocomplete_fields = ["company", "account"]
    
    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }
    
    fieldsets = (
        (_("General Information"), {
            "fields": ("name","company")
        }),
        (_("Contact Details"), {
            "fields": ("contact", ("phone", "email"), "associated_emails")
        }),
        (_("Finance"), {
            "fields": ("tax_identifier", "account")
        }),
    )


class PriceSheetItemInline(TabularInline):
    model = PriceSheetItem
    extra = 1
    autocomplete_fields = ("material", "component")
    fields = ("material", "component", "display_name", "order")
    ordering = ("order", "pk")
    verbose_name = _("Item")
    verbose_name_plural = _("Items")


@admin.register(PriceSheet)
class PriceSheetAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company", "created_at", "updated_at")
    search_fields = ("name",)
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
    )
    list_filter_submit = True
    autocomplete_fields = ("company",)
    inlines = [PriceSheetItemInline]

    fieldsets = (
        (None, {
            "fields": ("name", "company", "notes"),
        }),
    )


@admin.register(PriceSheetItem)
class PriceSheetItemAdmin(CompanyRestrictedAdmin, ModelAdmin, ImportExportModelAdmin):
    company_field_name = "price_sheet__company"
    resource_classes = [PriceSheetItemResource]
    export_form_class = SelectableFieldsExportForm

    list_display = ("resolved_reference", "resolved_name", "item_type", "resolved_unit_price", "price_sheet")
    list_filter = (
        ("price_sheet", AutocompleteSelectMultipleFilter),
    )
    list_filter_submit = True
    search_fields = ("material__name", "material__reference_code", "component__name", "component__code", "display_name")
    autocomplete_fields = ("price_sheet", "material", "component")
    list_select_related = ("price_sheet", "material", "material__provider", "material__category", "component")

    def resolved_reference(self, obj):
        return obj.resolved_reference
    resolved_reference.short_description = _("Reference")

    def resolved_name(self, obj):
        return obj.resolved_name
    resolved_name.short_description = _("Name")

    def item_type(self, obj):
        return obj.item_type
    item_type.short_description = _("Type")

    def resolved_unit_price(self, obj):
        return obj.resolved_unit_price
    resolved_unit_price.short_description = _("Unit Price")

    def get_queryset(self, request):
        qs = super(CompanyRestrictedAdmin, self).get_queryset(request)
        if request.user.is_superuser:
            return qs
        from core.admin.company_access import get_user_companies
        return qs.filter(price_sheet__company__in=get_user_companies(request.user))


from unfold.admin import ModelAdmin
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminTextInputWidget

from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)
from django_celery_beat.admin import ClockedScheduleAdmin as BaseClockedScheduleAdmin
from django_celery_beat.admin import CrontabScheduleAdmin as BaseCrontabScheduleAdmin
from django_celery_beat.admin import PeriodicTaskAdmin as BasePeriodicTaskAdmin
from django_celery_beat.admin import PeriodicTaskForm, TaskSelectWidget

admin.site.unregister(PeriodicTask)
admin.site.unregister(IntervalSchedule)
admin.site.unregister(CrontabSchedule)
admin.site.unregister(SolarSchedule)
admin.site.unregister(ClockedSchedule)


class UnfoldTaskSelectWidget(UnfoldAdminSelectWidget, TaskSelectWidget):
    pass


class UnfoldPeriodicTaskForm(PeriodicTaskForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["task"].widget = UnfoldAdminTextInputWidget()
        self.fields["regtask"].widget = UnfoldTaskSelectWidget()


@admin.register(PeriodicTask)
class PeriodicTaskAdmin(BasePeriodicTaskAdmin, ModelAdmin):
    form = UnfoldPeriodicTaskForm


@admin.register(IntervalSchedule)
class IntervalScheduleAdmin(ModelAdmin):
    pass


@admin.register(CrontabSchedule)
class CrontabScheduleAdmin(BaseCrontabScheduleAdmin, ModelAdmin):
    pass


@admin.register(SolarSchedule)
class SolarScheduleAdmin(ModelAdmin):
    pass

@admin.register(ClockedSchedule)
class ClockedScheduleAdmin(BaseClockedScheduleAdmin, ModelAdmin):
    pass