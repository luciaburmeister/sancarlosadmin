from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from django.utils.translation import gettext_lazy as _

from production.models import (
    ProductionTicket,
    ProductionItem,
)


class ProductionItemInline(TabularInline):
    model = ProductionItem
    extra = 0
    show_change_link = True
    tab=True

    fields = (
        "status",
        "planning",
        "data",
    )

@admin.register(ProductionTicket)
class ProductionTicketAdmin(ModelAdmin):
    list_display = (
        "id",
        "status",
        "created_at",
    )

    list_filter = (
        "status",
        "content_type",
    )

    search_fields = (
        "object_id",
    )

    inlines = [ProductionItemInline]

    readonly_fields = (
        "content_type",
        "object_id",
        "source_object",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "status",
                    ("content_type", "object_id"),
                    "source_object",
                    "context"
                )
            },
        ),

    )

    
