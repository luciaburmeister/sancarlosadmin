from django.contrib import admin
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from django.utils.translation import gettext_lazy as _
from assets.models import AssetPlanning


@admin.register(AssetPlanning)
class AssetPlanningAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin):
    list_display = (
        "asset", "origin", "destination",
        "assigned_to", "scheduled_date", "status", "company"
    )
    list_filter = ("status", "company", "origin", "destination")
    search_fields = (
    "asset__name",
    "assigned_to__first_name",
    "assigned_to__last_name",
    "origin__name",
    "destination__name",
    )
    autocomplete_fields = ["asset", "origin", "destination", "assigned_to", "company"]
    exclude = ["company"]
    fieldsets = (
        (_("Asset"), {
            "fields": (
                ("asset",),
            )
        }),
        (_("Route"), {
            "fields": (
                ("origin", "destination"),
                ("scheduled_date",),
            )
        }),
        (_("Assignment"), {
            "fields": (
                ("assigned_to", "status"),
                "notes",
            )
        }),
    )