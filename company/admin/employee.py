from unfold.admin import StackedInline
from django.contrib import admin, messages
from unfold.admin import ModelAdmin
from django.db import models

from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from company.models import Employee, EmployeeType, EmployeeDocument, EmployeeDocumentType
from django.utils.translation import gettext_lazy as _
from datetime import date

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display, action

from import_export.admin import ImportExportModelAdmin
from unfold.contrib.forms.widgets import ArrayWidget
from unfold.widgets import UnfoldAdminPasswordToggleWidget
from django.contrib.postgres.fields import ArrayField
from unfold.contrib.import_export.forms import (
    ExportForm,
    ImportForm,
    SelectableFieldsExportForm,
)

from core.admin.company_access import get_user_companies
from core.admin.company_admin import CompanyRestrictedAdmin
from company.models import Employee, EmployeeType
from company.views.employeesessions import EmployeeSessionsView
from company.views.driver_tracking import DriverTrackingView

from company.forms import BaseDocumentForm
from vacations.models import VacationWallet, VacationRequest, VacationRequestStatus
from decimal import Decimal
from django.db.models import Sum



class EmployeeDocumentForm(BaseDocumentForm):
    class Meta:
        model = EmployeeDocument
        fields = "__all__"


class EmployeeDocumentInline(StackedInline):
    model = EmployeeDocument
    form = EmployeeDocumentForm
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

@admin.register(EmployeeDocumentType)
class EmployeeDocumentTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]

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
    search_fields = ["last_name", "first_name", "email"]  # add this line


    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    actions = ["create_production_resources_action"]

    change_form_template = "admin/company/employee/change_form.html"

    list_display = (
        "full_name",
        "company",
        "is_active",
        "user",
        "current_working_zone",
    )

    list_filter = (
        "company",
        "is_active",
    )
    search_fields = (
        "first_name",
        "last_name",
        "email",
        "user__username",
        "user__email",
    )

    autocomplete_fields = (
        "company",
        "user",
        "sale_zones",
        "current_working_zone",
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
            "classes": ["tab"],
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
        "classes": ["tab"],
        "fields": (
            "identification_number",
            "birth_date",
            ("sex", "civil_status"),
            "birth_country",
            "home_street_address",
            ("home_city", "home_postal_code"),
            ("home_province", "home_country"),
        )
    },
    ),
    (
        _("Employment"),
        {
            "classes": ["tab"],
            "fields": (
                "company",
                ("hired_at", "terminated_at"),
                "is_active",
                "sale_zones",
                ("current_working_zone", "zone_changed_at"),
            )
        },
    ),
    (
        _("Production"),
        {
            "classes": ["tab"],
            "fields": (
                ("can_work_on_production", "is_production_supervisor"),
                "production_stages",
                "work_pin",
            )
        },
    ),
)
    inlines = (EmployeeDocumentInline, VacationRequestInline, VacationWalletInline)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "work_pin":
            kwargs["widget"] = UnfoldAdminPasswordToggleWidget(
                attrs={"maxlength": 4, "style": "max-width: 140px"}
            )
            kwargs["required"] = False
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if change and not form.cleaned_data.get("work_pin"):
            obj.work_pin = Employee.objects.get(pk=obj.pk).work_pin
        super().save_model(request, obj, form, change)

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
    readonly_fields = ("zone_changed_at",)

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

    @action(
        description=_("Create Production Resources"),
        icon="engineering",
    )
    def create_production_resources_action(self, request, queryset):
        from django.contrib.contenttypes.models import ContentType
        from production.models import ProductionResource

        employee_ct = ContentType.objects.get_for_model(Employee)

        existing_ids = set(
            ProductionResource.objects.filter(
                content_type=employee_ct,
                object_id__in=queryset.values_list("pk", flat=True),
            ).values_list("object_id", flat=True)
        )

        to_create = [
            ProductionResource(
                content_type=employee_ct,
                object_id=emp.pk,
                company=emp.company,
                is_active=True,
            )
            for emp in queryset
            if emp.pk not in existing_ids
        ]

        created_count = len(ProductionResource.objects.bulk_create(to_create))
        skipped_count = queryset.count() - created_count

        if created_count:
            messages.success(
                request,
                _("Created %(count)d production resource(s).") % {"count": created_count},
            )
        if skipped_count:
            messages.info(
                request,
                _("Skipped %(count)d employee(s) that already had a resource.") % {"count": skipped_count},
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
            path(
                "driver-tracking/",
                self.admin_site.admin_view(
                    DriverTrackingView.as_view(model_admin=self)
                ),
                name="company_employee_driver_tracking",
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
