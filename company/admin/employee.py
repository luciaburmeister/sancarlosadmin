from django.contrib import admin
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from datetime import date

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import (
    ExportForm,
    ImportForm,
    SelectableFieldsExportForm,
)

from core.admin.company_admin import CompanyRestrictedAdmin
from company.models import Employee, EmployeeType
from company.views.employeesessions import EmployeeSessionsView

from vacations.models import VacationWallet, VacationRequest, VacationRequestStatus
from decimal import Decimal
from django.db.models import Sum


class VacationWalletInline(TabularInline):
    model = VacationWallet
    extra = 0
    tab = True
    show_change_link = True

    fields = ("policy", "year", "balance", "accrued_ytd", "used_ytd", "updated_at")
    readonly_fields = ("balance", "accrued_ytd", "used_ytd", "updated_at")


class VacationRequestInline(TabularInline):
    model = VacationRequest
    extra = 0
    tab = True
    show_change_link = True

    fields = ("start_date", "end_date", "requested_days", "status", "approved_by")
    readonly_fields = ("requested_days", "approved_by")
    ordering = ("-created_at",)


@admin.register(Employee)
class EmployeeAdmin(ModelAdmin, CompanyRestrictedAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    export_form_class = SelectableFieldsExportForm

    change_form_template = "admin/company/employee/change_form.html"

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
                    ("type"),
                    "user",
                )
            },
        ),
        (
            _("Personal Information"),
            {
                "fields": (
                    "identification_number",
                    "birth_date",
                    ("sex", "civil_status"),
                    "birth_country",
                )
            },
        ),
        (
            _("Employment"),
            {
                "fields": (
                    "company",
                    ("hired_at", "terminated_at"),
                    ("is_active", "can_work_on_production"),
                )
            },
        ),
    )

    inlines = (VacationRequestInline, VacationWalletInline)
    readonly_fields = ()

    @display(description=_("Name"))
    def full_name(self, obj):
        return obj.full_name

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}

        extra_context["employee_sessions_url"] = reverse(
            "admin:company_employee_sessions",
            args=[object_id],
        )

        employee = self.get_queryset(request).get(pk=object_id)

        today = date.today()
        current_year = today.year

        wallet = (
            VacationWallet.objects.filter(employee=employee, year=current_year)
            .order_by("-year", "-id")
            .first()
        )

        if wallet:
            available = wallet.balance
            used = wallet.used_ytd

            pending = (
                VacationRequest.objects.filter(
                    wallet=wallet,
                    status=VacationRequestStatus.PENDING,
                )
                .aggregate(total=Sum("requested_days"))["total"]
            )
            if pending is None:
                pending = Decimal("0.00")
        else:
            available = used = None
            pending = Decimal("0.00")

        upcoming_requests = (
            VacationRequest.objects.select_related("approved_by")
            .filter(
                employee=employee,
                status__in=[
                    VacationRequestStatus.PENDING,
                    VacationRequestStatus.APPROVED,
                ],
                end_date__gte=today,
            )
            .order_by("start_date")[:5]
        )

        vacation_request_add_url = (
            reverse("admin:vacations_vacationrequest_add") + f"?employee={employee.pk}"
        )

        extra_context["vacation_dashboard"] = {
            "wallet": wallet,
            "available": available,
            "pending": pending,
            "used": used,
            "upcoming_requests": upcoming_requests,
            "vacation_request_add_url": vacation_request_add_url,
        }

        return super().change_view(
            request, object_id, form_url, extra_context=extra_context
        )

    def get_urls(self):
        return [
            path(
                "<int:employee_id>/sessions/",
                self.admin_site.admin_view(
                    EmployeeSessionsView.as_view(model_admin=self)
                ),
                name="company_employee_sessions",
            ),
        ] + super().get_urls()

    def get_inline_instances(self, request, obj=None):
        instances = super().get_inline_instances(request, obj)

        is_manager = (
            request.user.is_superuser
            or request.user.has_perm("vacations.can_view_all_vacations")
            or request.user.has_perm("vacations.can_approve_vacations")
        )

        if not is_manager:
            instances = [i for i in instances if i.model != VacationRequest]

        return instances


@admin.register(EmployeeType)
class EmployeeTypeAdmin(ModelAdmin):
    list_display = ("type", "company")
    search_fields = ("type",)
