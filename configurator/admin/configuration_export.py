from django.contrib import admin
from unfold.admin import ModelAdmin
from django.db import models
from configurator.models import ConfigurationExportTemplate
from unfold.contrib.forms.widgets import WysiwygWidget
from core.admin.company_admin import CompanyRestrictedAdmin

@admin.register(ConfigurationExportTemplate)
class ConfigurationExportTemplateAdmin(CompanyRestrictedAdmin):
    list_display = ("name", "product", "is_active","company")
    list_filter = ("is_active", "product")
    search_fields = ("name", "html")
    readonly_fields = ()
    autocomplete_fields = ["product","company"]
    formfield_overrides = {
        models.TextField: {
            "widget": WysiwygWidget,
        }
    }
    list_select_related = ["company"]

    fieldsets = (
        ("Template Info", {
            "fields": ("name", ("product", "is_active"),"company")
        }),
        ("Template Content", {
            "fields": ("html","css"),
        }),
    )

