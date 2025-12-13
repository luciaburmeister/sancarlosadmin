from django.contrib import admin
from unfold.admin import ModelAdmin
from django.db import models
from configurator.models import ConfigurationExportTemplate
from unfold.contrib.forms.widgets import WysiwygWidget

@admin.register(ConfigurationExportTemplate)
class ConfigurationExportTemplateAdmin(ModelAdmin):
    list_display = ("name", "product", "is_active")
    list_filter = ("is_active", "product")
    search_fields = ("name", "html")
    readonly_fields = ()
    autocomplete_fields = ["product"]
    formfield_overrides = {
        models.TextField: {
            "widget": WysiwygWidget,
        }
    }

    fieldsets = (
        ("Template Info", {
            "fields": ("name", ("product", "is_active"))
        }),
        ("Template Content", {
            "fields": ("html","css"),
        }),
    )

