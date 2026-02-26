from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import ProductParameter


@admin.register(ProductParameter)
class ProductParameterAdmin(ModelAdmin):
    list_display = ["id", "product", "name", "label", "type", "required", "default_value"]
    list_filter = ("product", "type", "required")
    search_fields = ("name", "label", "product__name")
    hide_ordering_field = True
    ordering_field = "order"

    fieldsets = (
        ("Parameter", {
            "fields": ("product", "name", "label", "type", "required", "default_value", "options"),
        }),
    )
