from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
from django.contrib import admin
from company.models import WorkingZone, WorkingZoneAuthorization
from unfold.admin import ModelAdmin, TabularInline
from assets.models import AssetTransaction
from django.utils.translation import gettext_lazy as _


class AssetTransactionInline(TabularInline):
    model = AssetTransaction
    fk_name = "to_zone"
    extra = 0
    tab = True
    fields = ("asset", "transaction_type", "from_zone", "project_file", "date", "notes")
    readonly_fields = ("created_at",)
    ordering = ("-date",)
    autocomplete_fields = ["asset", "from_zone", "project_file"]
    per_page = 10


class WorkingZoneAuthorizationInline(TabularInline):
    model = WorkingZoneAuthorization
    extra = 1
    tab = True
    verbose_name = _("Authorized Employee")
    verbose_name_plural = _("Authorized Employees")
    fields = ("employee",)
    autocomplete_fields = ["employee"]


@admin.register(WorkingZone)
class WorkingZoneAdmin(ModelAdmin):
    list_display = ("name", "company", "client", "project_file", "city", "province", "zone_type", "is_active")
    search_fields = (
        "name", "street_address", "city", "cnae", "nima", "cif",
        "company__name", "client__name",
    )
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        "is_active",
        ("client", AutocompleteSelectMultipleFilter),
        ("zone_type", MultipleChoicesDropdownFilter),
    )
    autocomplete_fields = ["company", "project_file", "client"]
    list_select_related = ["company", "client", "project_file__client"]
    list_filter_submit = True
    readonly_fields = ("location_confirmed_at",)
    inlines = [AssetTransactionInline, WorkingZoneAuthorizationInline]
    fieldsets = (
        (None, {
            "fields": (
                "name",
                "company",
                "client",
                "project_file",
                "zone_type",
                "cnae",
                "nima",
                "cif",
                "is_active",
            )
        }),
        (_("Receipt email"), {
            "fields": ("receipt_notification_email", "auto_send_receipt"),
        }),
        ("Location", {
            "fields": (
                "street_address",
                "city",
                "postal_code",
                "province",
                "country",
                "location",
                "location_confirmed_at",
            )
        }),
    )