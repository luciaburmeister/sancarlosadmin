from django.contrib import admin
from company.models import WorkingZone
from unfold.admin import ModelAdmin, TabularInline
from assets.models import AssetTransaction


class AssetTransactionInline(TabularInline):
    model = AssetTransaction
    fk_name = "to_zone"
    extra = 0
    tab = True
    fields = ("asset", "transaction_type", "from_zone", "project_file", "date", "notes")
    readonly_fields = ("created_at",)
    ordering = ("-date",)


@admin.register(WorkingZone)
class WorkingZoneAdmin(ModelAdmin):
    list_display = ("name", "company", "client", "project_file", "city", "province", "is_active")
    search_fields = ("name", "street_address", "city", "company__name", "client__name")
    list_filter = ("company", "is_active", "client")
    autocomplete_fields = ["company", "project_file", "client"]
    inlines = [AssetTransactionInline]
    fieldsets = (
        (None, {
            "fields": (
                "name",
                "company",
                "client",
                "project_file",
                "zone_type",
                "is_active",
            )
        }),
        ("Location", {
            "fields": (
                "street_address",
                "city",
                "postal_code",
                "province",
                "country",
                "location",
            )
        }),
    )