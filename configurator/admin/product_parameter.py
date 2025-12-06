from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import ProductParameter


@admin.register(ProductParameter)
class ProductParameterAdmin(ModelAdmin):
    list_display = ("id", "product", "name", "label", "type", "required")
    list_filter = ("product", "type", "required")
    search_fields = ("name", "label", "product__name")

    fieldsets = (
        ("Parameter", {
            "fields": ("product", "name", "label", "type", "required", "options"),
        }),
    )
