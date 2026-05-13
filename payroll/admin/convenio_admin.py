from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from payroll.models.convenio import (
    CollectiveAgreement,
    CollectiveAgreementCategory,
    CollectiveAgreementExtraPay,
    CollectiveAgreementSeniority,
)


@admin.register(CollectiveAgreementCategory)
class CollectiveAgreementCategoryAdmin(ModelAdmin):
    search_fields = (
        "code",
        "official_name",
        "collective_agreement__description",
        "collective_agreement__official_code",
    )
    list_display = ("id", "code", "official_name", "collective_agreement")
    list_filter = ("collective_agreement",)
    autocomplete_fields = ("collective_agreement",)
    ordering = ("collective_agreement", "code")


class CollectiveAgreementCategoryInline(TabularInline):
    model = CollectiveAgreementCategory
    extra = 0
    tab = True
    fields = (
        "code",
        "official_name",
        "base_salary",
        "collective_agreement_allowance",
        "contribution_group",
        "temporary_disability_percentage",
        "voluntary_ss_supplement_percentage",
    )
    verbose_name = _("Category")
    verbose_name_plural = _("Categories")


class CollectiveAgreementExtraPayInline(TabularInline):
    model = CollectiveAgreementExtraPay
    extra = 0
    tab = True
    fields = (
        "number",
        "name",
        "type",
        "payment_date",
        "accrual_start",
        "accrual_end",
        "temporary_disability_discount_percentage",
        "prorate_on_hire_and_termination",
    )
    verbose_name = _("Extra Payment")
    verbose_name_plural = _("Extra Payments")


class CollectiveAgreementSeniorityInline(TabularInline):
    model = CollectiveAgreementSeniority
    extra = 0
    tab = True
    fields = ("year", "percentage", "amount")
    verbose_name = _("Seniority Year")
    verbose_name_plural = _("Seniority Table")


@admin.register(CollectiveAgreement)
class CollectiveAgreementAdmin(ModelAdmin):
    list_display = (
        "official_code",
        "description",
        "show_active",
        "valid_from",
        "valid_until",
        "professional_classification",
    )
    list_filter = ("is_active", "professional_classification")
    search_fields = ("official_code", "description")
    ordering = ("description",)

    fieldsets = (
        (
            _("Identification"),
            {
                "fields": (
                    ("official_code", "description"),
                    "publication_date",
                    "professional_classification",
                    "seniority_by_classification",
                )
            },
        ),
        (
            _("Validity"),
            {
                "fields": (
                    ("valid_from", "valid_until"),
                    "is_active",
                )
            },
        ),
        (
            _("Levels"),
            {
                "fields": (
                    "extra_payments_level",
                    "seniority_level",
                )
            },
        ),
    )

    inlines = [
        CollectiveAgreementCategoryInline,
        CollectiveAgreementExtraPayInline,
        CollectiveAgreementSeniorityInline,
    ]

    @display(
        description=_("Active"),
        label={
            True: "success",
            False: "danger",
        },
        boolean=True,
    )
    def show_active(self, obj):
        return obj.is_active