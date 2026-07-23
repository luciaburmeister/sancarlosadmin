from django.contrib import admin
from django.contrib.postgres.fields import ArrayField
from unfold.admin import ModelAdmin, TabularInline
from core.admin.company_admin import CompanyRestrictedAdmin
from django.db import models
from django.utils.translation import gettext_lazy as _
from unfold.contrib.forms.widgets import ArrayWidget
from unfold.widgets import UnfoldAdminTextareaWidget
from ..models import AssetType, AssetTypeField


class AssetTypeFieldInline(TabularInline):
    model = AssetTypeField
    extra = 0
    tab = True
    ordering_field = "order"
    hide_ordering_field = True
    fields = ("name", "label", "type", "required", "options", "order")

    formfield_overrides = {
        models.JSONField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }


@admin.register(AssetType)
class AssetTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "description", "fields_summary")
    search_fields = ("name",)
    inlines = [AssetTypeFieldInline]
    readonly_fields = ("schema",)
    formfield_overrides = {
        ArrayField: {"widget": ArrayWidget},
    }
    fieldsets = (
        (None, {"fields": ("name", "description", "company", "identifier_labels")}),
        (_("Validation Schema"), {"fields": ("schema",)}),
    )

    def fields_summary(self, obj):
        return ", ".join(obj.fields.values_list("label", flat=True))
    fields_summary.short_description = _("Fields")

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("fields")
