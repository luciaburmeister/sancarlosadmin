from django.contrib import admin
from unfold.admin import ModelAdmin

from company.models import Company, CompanyUser


@admin.register(Company)
class CompanyAdmin(ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)

    ordering = ("name",)

@admin.register(CompanyUser)
class CompanyUserAdmin(ModelAdmin):
    list_display = ("user", "company", "is_active")
    list_filter = ("company", "is_active")
    search_fields = (
        "user__username",
        "user__email",
        "company__name",
    )

    autocomplete_fields = ("user", "company")

    ordering = ("company", "user")
