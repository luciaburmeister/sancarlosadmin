from django.contrib import admin
from django.db.models import BooleanField, Case, Q, Value, When
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import display

from apps.reports.constants import ReportGenerationStatus, ReportVisibility
from apps.reports.models import Report
from apps.reports.views import (
    ReportConfirmAdminView,
    ReportCreateAdminView,
    ReportEditAdminView,
    ReportRunAdminView,
)
from core.admin.company_access import get_user_companies
from core.admin.company_admin import CompanyRestrictedAdmin


@admin.register(Report)
class ReportAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "name_link",
        "generation_status",
        "domain",
        "visibility",
        "owner",
        "company",
        "updated_at",
        "open_link",
        "edit_link",
    )
    list_display_links = None
    list_filter = ("status", "domain", "visibility")
    list_select_related = ("owner", "company")
    search_fields = ("name", "prompt")
    readonly_fields = (
        "owner",
        "domain",
        "spec",
        "prompt",
        "status",
        "generation_summary",
        "generation_error",
        "created_at",
        "updated_at",
    )
    fields = (
        "name",
        "visibility",
        "owner",
        "domain",
        "prompt",
        "spec",
        "status",
        "generation_summary",
        "generation_error",
        "company",
        "created_at",
        "updated_at",
    )

    def get_queryset(self, request):
        scoped = super().get_queryset(request)
        included = admin.ModelAdmin.get_queryset(self, request).filter(
            included_companies__in=get_user_companies(request.user)
        )
        return (
            (scoped | included)
            .filter(Q(owner=request.user) | Q(visibility=ReportVisibility.COMPANY))
            .distinct()
            .annotate(
                _can_edit=Case(
                    When(owner=request.user, then=Value(True)),
                    default=Value(request.user.is_superuser),
                    output_field=BooleanField(),
                )
            )
        )

    def has_module_permission(self, request):
        return request.user.has_perm("reports.use_reports")

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("reports.use_reports")

    def has_add_permission(self, request):
        return request.user.has_perm("reports.use_reports")

    def has_change_permission(self, request, obj=None):
        if not request.user.has_perm("reports.use_reports"):
            return False
        if obj is None:
            return True
        return obj.owner_id == request.user.pk or request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        if not request.user.has_perm("reports.use_reports"):
            return False
        if obj is None:
            return True
        return obj.owner_id == request.user.pk or request.user.is_superuser

    def add_view(self, request, form_url="", extra_context=None):
        return redirect("admin:reports_report_create")

    @display(
        description=_("Status"),
        label={
            "Processing": "warning",
            "Confirm": "info",
            "Ready": "success",
            "Error": "danger",
            "Needs detail": "info",
        },
    )
    def generation_status(self, obj):
        return {
            ReportGenerationStatus.PROCESSING: "Processing",
            ReportGenerationStatus.CONFIRM: "Confirm",
            ReportGenerationStatus.READY: "Ready",
            ReportGenerationStatus.ERROR: "Error",
            ReportGenerationStatus.CLARIFY: "Needs detail",
        }.get(obj.status, obj.status)

    @display(description=_("Name"), ordering="name")
    def name_link(self, obj):
        url = reverse("admin:reports_report_run", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.name)

    @display(description=_("Open"))
    def open_link(self, obj):
        url = reverse("admin:reports_report_change", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, _("Open"))

    @display(description=_("Edit"))
    def edit_link(self, obj):
        if not obj._can_edit:
            return "—"
        url = reverse("admin:reports_report_edit", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, _("Edit"))

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "create/",
                self.admin_site.admin_view(
                    ReportCreateAdminView.as_view(model_admin=self)
                ),
                name="reports_report_create",
            ),
            path(
                "<path:object_id>/confirm/",
                self.admin_site.admin_view(
                    ReportConfirmAdminView.as_view(model_admin=self)
                ),
                name="reports_report_confirm",
            ),
            path(
                "<path:object_id>/run/",
                self.admin_site.admin_view(
                    ReportRunAdminView.as_view(model_admin=self)
                ),
                name="reports_report_run",
            ),
            path(
                "<path:object_id>/edit/",
                self.admin_site.admin_view(
                    ReportEditAdminView.as_view(model_admin=self)
                ),
                name="reports_report_edit",
            ),
        ]
        return custom + urls
