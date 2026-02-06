from django.contrib import admin
from unfold.admin import ModelAdmin

from company.models import Company, CompanyUser
from django.utils.translation import gettext_lazy as _


from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField

@admin.register(Company)
class CompanyAdmin(ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)

    ordering = ("name",)

    fieldsets = (
        (None, {
            "fields": (
                "name",
                "legal_name",
                "is_active",
                "logo",
                "address",
                "email",
                "phone",
                "tax_identifier",
                "constituted_country",
                "production_stages",
            )
        }),
    )

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

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
