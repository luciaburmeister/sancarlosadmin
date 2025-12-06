from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import ConfigurationParameterValue


@admin.register(ConfigurationParameterValue)
class ConfigurationParameterValueAdmin(ModelAdmin):
    list_display = ("id", "configuration", "parameter", "value")
    list_filter = ("parameter", "configuration")
    search_fields = ("parameter__name", "value", "configuration__name")

    fieldsets = (
        ("Parameter Value", {
            "fields": ("configuration", "parameter", "value"),
        }),
    )
