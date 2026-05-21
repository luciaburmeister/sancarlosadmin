from unfold.admin import StackedInline, TabularInline
from django.contrib import admin
from unfold.admin import ModelAdmin
from django.db import models
from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from company.models import Machine, MachineType, MachineDocument, MachineDocumentType
from company.forms import BaseDocumentForm
from core.admin.company_access import get_user_companies


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
    autocomplete_fields = ["document_type"]

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
class MachineDocumentTypeAdmin(ModelAdmin, CompanyRestrictedAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]

@admin.register(Machine)
class MachineAdmin(ModelAdmin, CompanyRestrictedAdmin):
    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

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

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)

        if obj and obj.company:
            stages = obj.company.production_stages or []
        else:
            stages = []
            for company in get_user_companies(request.user):
                stages.extend(company.production_stages or [])

            # remove duplicates while preserving order
            stages = list(dict.fromkeys(stages))

        form.base_fields["production_stages"].widget = ArrayWidget(
            choices=[(s, s) for s in stages]
        )
        return form

@admin.register(MachineType)
class MachineTypeAdmin(ModelAdmin):
    list_display = ("type", "company")
    search_fields = ("type",)
