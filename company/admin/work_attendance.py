from django.contrib import admin
from unfold.admin import ModelAdmin
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from company.models import WorkAttendance
from unfold.contrib.filters.admin import RangeDateFilter


@admin.register(WorkAttendance)
class WorkAttendanceAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "employee", 
        "clock_in_time",
        "clock_out_time",
        "clock_in_location",
        "clock_out_location",
    )
    list_filter = (
        ("clock_in_time", RangeDateFilter),   # ← filter by day range
        "employee",
    )
    search_fields = (
        "employee__first_name",
        "employee__last_name",
        "employee__user__username",
    )
    readonly_fields = (
        "clock_in_time", "clock_out_time",
        "clock_in_lat", "clock_in_lng",
        "clock_out_lat", "clock_out_lng",
    )
    exclude = ["company"]
    fieldsets = (
        (_("Employee"), {
            "fields": ("employee",)
        }),
        (_("Clock In"), {
            "fields": (
                ("clock_in_time",),
                ("clock_in_lat", "clock_in_lng"),
            )
        }),
        (_("Clock Out"), {
            "fields": (
                ("clock_out_time",),
                ("clock_out_lat", "clock_out_lng"),
            )
        }),
    )

    def clock_in_location(self, obj):
        if obj.clock_in_lat and obj.clock_in_lng:
            return f"{obj.clock_in_lat}, {obj.clock_in_lng}"
        return "—"
    clock_in_location.short_description = _("Clock In Location")

    def clock_out_location(self, obj):
        if obj.clock_out_lat and obj.clock_out_lng:
            return f"{obj.clock_out_lat}, {obj.clock_out_lng}"
        return "—"
    clock_out_location.short_description = _("Clock Out Location")