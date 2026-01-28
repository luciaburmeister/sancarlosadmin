from django.contrib import admin
from unfold.admin import ModelAdmin

from company.models import Employee
from django.utils.translation import gettext_lazy as _

@admin.register(Employee)
class EmployeeAdmin(ModelAdmin):
    list_display = (
        "full_name",
        "company",
        "is_active",
        "user",
    )

    list_filter = (
        "company",
        "is_active",
    )

    search_fields = (
        "first_name",
        "last_name",
        "email",
        "employee_id",
        "user__username",
        "user__email",
    )

    autocomplete_fields = (
        "company",
        "user",
    )

    ordering = (
        "company",
        "last_name",
        "first_name",
    )

    fieldsets = (
        (
            _("Employee"),
            {
                "fields": (
                    ("first_name", "last_name"),
                    ("email", "phone"),
                    ("user")
                )
            },
        ),
        (
            _("Employment"),
            {
                "fields": (
                    "company",
                    ("hired_at", "terminated_at"),
                    "is_active",
                )
            },
        )
    )

    readonly_fields = ()

    def full_name(self, obj):
        return obj.full_name

    full_name.short_description = "Name"