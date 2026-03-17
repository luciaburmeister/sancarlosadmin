from unfold.admin import StackedInline, TabularInline
from django.contrib import admin
from unfold.admin import ModelAdmin
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from company.models import Machine, MachineType, MachineDocument, MachineDocumentType
from company.forms import BaseDocumentForm


class MachineDocumentForm(BaseDocumentForm):
    class Meta:
        model = MachineDocument
        fields = "__all__"


from company.forms import BaseDocumentForm


class MachineDocumentForm(BaseDocumentForm):
    class Meta:
        model = MachineDocument
        fields = "__all__"


class MachineDocumentInline(StackedInline):
    model = MachineDocument
    form = MachineDocumentForm
    extra = 0
    tab = True
    exclude = ["company"]

    fieldsets = (
        (None, {
            "fields": (
                ("document_type",  "file"),
                ("does_not_expire","is_active"),
                ("issue_date", "expiry_date"),
                "notes",
            )
        }),
    )

@admin.register(MachineDocumentType)
class MachineDocumentTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]

@admin.register(Machine)
class MachineAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "name",
        "company",
        "code",
        "is_active",
    )

    inlines = [MachineDocumentInline]

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
