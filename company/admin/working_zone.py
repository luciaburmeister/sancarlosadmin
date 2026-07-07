from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
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
    autocomplete_fields = ["asset","from_zone","project_file"]
    per_page=10


@admin.register(WorkingZone)
class WorkingZoneAdmin(ModelAdmin):
    list_display = ("name", "company", "client", "project_file", "city", "province", "is_active")
    search_fields = ("name", "street_address", "city", "company__name", "client__name")
    list_filter = (("company",AutocompleteSelectMultipleFilter), "is_active", ("client",AutocompleteSelectMultipleFilter))
    autocomplete_fields = ["company", "project_file", "client"]
    list_selected_related = ["company","project_file__company"]
    list_filter_submit = True
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