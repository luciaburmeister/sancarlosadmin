from unfold.admin import StackedInline
from django.contrib import admin
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from company.models import Machine, MachineType, MachineDocument, MachineDocumentType

class MachineDocumentInline(StackedInline):
    model = MachineDocument
    extra = 0
    tab=True
    exclude = ["company"]

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
