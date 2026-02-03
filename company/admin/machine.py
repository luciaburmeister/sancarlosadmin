from django.contrib import admin
from unfold.admin import ModelAdmin

from company.models import Machine, MachineType

@admin.register(Machine)
class MachineAdmin(ModelAdmin):
    list_display = (
        "name",
        "company",
        "code",
        "is_active",
    )

    list_filter = (
        "company",
        "is_active",
    )

    search_fields = (
        "name",
        "code",
    )

    autocomplete_fields = (
        "company",
        "type",
    )

    ordering = (
        "company",
        "type",
        "name",
        "code",
    )

@admin.register(MachineType)
class MachineTypeAdmin(ModelAdmin):
    list_display = ("type", "company")
    search_fields = ("type",)
