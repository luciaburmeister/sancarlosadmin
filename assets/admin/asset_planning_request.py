from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.db import transaction
from django.urls import reverse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action, display
from unfold.enums import ActionVariant
from simple_history.admin import SimpleHistoryAdmin

from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.company_access import get_user_companies
from company.models import Employee
from assets.constants import (
    SERVICE_TYPE_FIELDS, SERVICE_TYPE_MAQUINA_PROPIA, SERVICE_TYPE_MAQUINISTAS,
    SERVICE_TYPE_AUTOPORTE_MAQUINA, SERVICE_TYPE_PEONES, SERVICE_TYPE_PLANNING_TYPE,
)
from assets.forms import AssetPlanningRequestForm
from assets.models import (
    Asset, AssetPlanning, AssetPlanningRequest, AssetPlanningRequestDraft,
    AssetPlanningRequestDraftStatus, AssetPlanningRequestStatus, AssetTypeCategory, PlanningType,
)

# Built from SERVICE_TYPE_FIELDS so the form and AssetPlanningRequest.clean()
# never disagree about which fields apply to which service type.
_CONDITIONAL_FIELD_NAMES = ["from_zone", "asset", "machine", "load_description", "cuba_operation_type", "cuba_size"]
CONDITIONAL_FIELDS = {
    field_name: " || ".join(
        f"service_type == '{service_type}'"
        for service_type, fields in SERVICE_TYPE_FIELDS.items()
        if field_name in fields
    )
    for field_name in _CONDITIONAL_FIELD_NAMES
}
# Not in SERVICE_TYPE_FIELDS — shared with AssetPlanningRequestDraft, which lacks these fields.
_NO_PLANNING_SERVICE_TYPES = [st for st, pt in SERVICE_TYPE_PLANNING_TYPE.items() if pt == PlanningType.NONE]
CONDITIONAL_FIELDS["estimated_duration"] = " && ".join(
    f"service_type != '{service_type}'" for service_type in _NO_PLANNING_SERVICE_TYPES
)
CONDITIONAL_FIELDS["machinist_moved"] = f"service_type == '{SERVICE_TYPE_MAQUINA_PROPIA}'"
CONDITIONAL_FIELDS["moved_employees"] = (
    f"(service_type == '{SERVICE_TYPE_MAQUINA_PROPIA}' && machinist_moved) "
    f"|| service_type == '{SERVICE_TYPE_MAQUINISTAS}' "
    f"|| service_type == '{SERVICE_TYPE_AUTOPORTE_MAQUINA}' "
    f"|| service_type == '{SERVICE_TYPE_PEONES}'"
)


class ChangesPendingFilter(admin.SimpleListFilter):
    title = _("Review state")
    parameter_name = "review_state"

    def lookups(self, request, model_admin):
        return [
            ("changes_pending", _("Changes pending")),
            ("first_review", _("First review")),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if value == "changes_pending":
            return queryset.filter(
                status=AssetPlanningRequestStatus.PENDING_REVIEW,
                resulting_planning__isnull=False,
            )
        if value == "first_review":
            return queryset.filter(
                status=AssetPlanningRequestStatus.PENDING_REVIEW,
                resulting_planning__isnull=True,
            )
        return queryset


@admin.register(AssetPlanningRequest)
class AssetPlanningRequestAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin):
    form = AssetPlanningRequestForm
    change_form_before_template = "admin/assets/assetplanningrequest/source_draft_panel.html"
    list_display = (
        "service_type", "to_zone", "from_zone",
        "needed_date", "needed_time", "urgency", "show_status",
        "requested_by", "assigned_to", "asset", "machine", "company",
    )
    list_filter = (ChangesPendingFilter, "status", "urgency", "service_type", "company")
    search_fields = (
        "to_zone__name", "from_zone__name",
        "requested_by__first_name", "requested_by__last_name",
        "assigned_to__first_name", "assigned_to__last_name",
        "asset__name", "machine__name",
    )
    autocomplete_fields = [
        "company",
        "project_file",
        "to_zone",
        "from_zone",
        "asset",
        "machine",
        "assigned_to",
        "suggested_trucks",
        "transport_asset",
        "moved_employees",
        "resulting_planning",
    ]
    readonly_fields = [
        "planning_type", "requested_by", "status",
        "reviewed_by", "reviewed_at",
        "resulting_planning", "zone_sync_applied_at",
        "rereview_notice",
    ]
    actions_row = ["duplicate_action"]
    actions_submit_line = ["approve_submit_action", "reject_submit_action"]
    conditional_fields = CONDITIONAL_FIELDS

    fieldsets = (
        (_("Request"), {
            "fields": (
                "service_type", "planning_type",
                ("to_zone", "from_zone"),
                ("needed_date", "needed_time"),
                "estimated_duration",
                "urgency",
                ("cuba_operation_type", "cuba_size"),
                ("machinist_moved", "moved_employees"),
                "load_description", "notes",
                ("asset", "machine", "assigned_to"),
                ("suggested_trucks", "transport_asset"),
            )
        }),
        (_("Review"), {
            "fields": (
                "status", "rereview_notice", "requested_by",
                ("reviewed_by", "reviewed_at"),
                "rejection_reason",
                "resulting_planning", "zone_sync_applied_at",
            )
        }),
    )

    def get_fieldsets(self, request, obj=None):
        request_fields = self.fieldsets[0][1]["fields"]
        # Company (superuser) then Project File, then the rest of the request fields.
        leading = ("company", "project_file") if request.user.is_superuser else ("project_file",)
        return (
            (self.fieldsets[0][0], {"fields": leading + request_fields}),
            self.fieldsets[1],
        )

    def get_form(self, request, obj=None, **kwargs):
        # Stashed for formfield_for_foreignkey/formfield_for_manytomany below.
        request._obj_ = obj
        return super().get_form(request, obj, **kwargs)

    def add_view(self, request, form_url="", extra_context=None):
        # Stashed for get_changeform_initial_data below and rendered by
        # change_form_before_template (source_draft_panel.html) so the
        # planner can see the original mobile draft while filling in the
        # real zones/project file it doesn't carry.
        draft_id = request.GET.get("draft_id")
        if draft_id:
            request._source_draft = AssetPlanningRequestDraft.objects.filter(
                pk=draft_id
            ).select_related("client", "asset", "machine", "requested_by").first()
            if request._source_draft is not None:
                extra_context = extra_context or {}
                extra_context["source_draft"] = request._source_draft
        return super().add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        # Same source_draft_panel.html as add_view, so the second reviewer
        # can also see the original mobile draft, not just the first.
        obj = self.get_object(request, unquote(object_id))
        source_draft = getattr(obj, "source_draft", None) if obj is not None else None
        if source_draft is not None:
            extra_context = extra_context or {}
            extra_context["source_draft"] = source_draft
        return super().change_view(request, object_id, form_url, extra_context)

    def get_changeform_initial_data(self, request):
        # Prefills the add form from a mobile draft (see add_view above and
        # AssetPlanningRequestDraftAdmin.create_request_action).
        initial = super().get_changeform_initial_data(request)
        draft = getattr(request, "_source_draft", None)
        if draft is None:
            return initial
        initial.update({
            "service_type": draft.service_type,
            "needed_date": draft.needed_date,
            "cuba_operation_type": draft.cuba_operation_type,
            "cuba_size": draft.cuba_size,
            "asset": draft.asset_id,
            "machine": draft.machine_id,
            "load_description": draft.load_description,
            "notes": draft.notes,
        })
        # Optional on the draft; only override the request's own default
        # (urgency=MEDIUM) when the requester actually picked one.
        if draft.needed_time:
            initial["needed_time"] = draft.needed_time
        if draft.urgency:
            initial["urgency"] = draft.urgency
        # Zones the requester picked from the real catalog carry straight
        # over — only the new-zone-request suggestions (destination_text/
        # destination_city, origin_text/origin_city) need the reviewer to
        # create a WorkingZone by hand first (see source_draft_panel.html).
        if draft.to_zone_id:
            initial["to_zone"] = draft.to_zone_id
        if draft.from_zone_id:
            initial["from_zone"] = draft.from_zone_id
        moved_employee_ids = list(draft.moved_employees.values_list("id", flat=True))
        if moved_employee_ids:
            initial["moved_employees"] = moved_employee_ids
        return initial

    def response_add(self, request, obj, post_url_continue=None):
        draft_id = request.GET.get("draft_id")
        if draft_id:
            AssetPlanningRequestDraft.objects.filter(pk=draft_id).update(
                status=AssetPlanningRequestDraftStatus.PROCESSED, resulting_request=obj,
            )
        return super().response_add(request, obj, post_url_continue)

    def _target_company(self, request):
        """Company every company-scoped field (asset, assigned_to, suggested_trucks) must agree on."""
        obj = getattr(request, "_obj_", None)
        if obj is not None and obj.company_id:
            return obj.company
        employee = Employee.objects.filter(user=request.user).first()
        if employee and employee.company_id:
            return employee.company
        return get_user_companies(request.user).order_by("name").first()

    def save_model(self, request, obj, form, change):
        if not change:
            if not obj.requested_by_id:
                obj.requested_by = Employee.objects.filter(user=request.user).first()
            if not obj.company_id:
                obj.company = self._target_company(request)
        super().save_model(request, obj, form, change)
        if not change:
            from assets.tasks import send_asset_planning_request_created_notification
            transaction.on_commit(
                lambda: send_asset_planning_request_created_notification.delay(obj.pk)
            )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Restricted to every company the user has access to (not just their
        # "default" one) — the same set CompanyRestrictedAdmin uses for the
        # asset/employee/project-file autocomplete search. A narrower,
        # single-company filter here would let the autocomplete surface a
        # choice this queryset then rejects as invalid on save. clean()
        # still rejects mismatched company combinations.
        companies = None if request.user.is_superuser else get_user_companies(request.user)
        if db_field.name in ("asset", "machine"):
            qs = Asset.objects.filter(asset_type__category=AssetTypeCategory.MACHINE, is_active=True)
            if companies is not None:
                qs = qs.filter(company__in=companies)
            kwargs["queryset"] = qs.order_by("name")
        elif db_field.name == "assigned_to":
            qs = Employee.objects.all()
            if companies is not None:
                qs = qs.filter(company__in=companies)
            kwargs["queryset"] = qs.order_by("first_name", "last_name")
        elif db_field.name == "transport_asset":
            qs = Asset.objects.filter(asset_type__category=AssetTypeCategory.TRUCK, is_active=True)
            if companies is not None:
                qs = qs.filter(company__in=companies)
            obj = getattr(request, "_obj_", None)
            if obj is not None and obj.pk:
                suggested_ids = obj.suggested_trucks.values_list("pk", flat=True)
                if suggested_ids:
                    qs = qs.filter(pk__in=suggested_ids)
            kwargs["queryset"] = qs.order_by("name")
        elif db_field.name == "project_file":
            from management.models import ProjectFile
            qs = ProjectFile.objects.all()
            if companies is not None:
                qs = qs.filter(company__in=companies)
            kwargs["queryset"] = qs.order_by("reference", "name")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        companies = None if request.user.is_superuser else get_user_companies(request.user)
        if db_field.name == "suggested_trucks":
            qs = Asset.objects.filter(asset_type__category=AssetTypeCategory.TRUCK, is_active=True)
            if companies is not None:
                qs = qs.filter(company__in=companies)
            kwargs["queryset"] = qs.order_by("name")
        elif db_field.name == "moved_employees":
            qs = Employee.objects.filter(is_active=True)
            if companies is not None:
                qs = qs.filter(company__in=companies)
            kwargs["queryset"] = qs.order_by("first_name", "last_name")
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    @display(
        description=_("Status"),
        ordering="status",
        label={
            AssetPlanningRequestStatus.PENDING_REVIEW.label: "warning",
            _("Changes pending"): "info",
            AssetPlanningRequestStatus.APPROVED.label: "success",
            AssetPlanningRequestStatus.REJECTED.label: "danger",
        },
    )
    def show_status(self, obj):
        if obj.awaits_rereview:
            return _("Changes pending")
        return obj.get_status_display()

    @display(description=_("Notice"))
    def rereview_notice(self, obj):
        if not obj or not obj.awaits_rereview:
            return "—"
        return _(
            "Previously approved plan still exists (see Asset Planning below) — "
            "that is the old plan to replace. Re-approve will create a new plan "
            "and cancel the old one."
        )

    def has_approve_permission(self, request, object_id=None):
        return request.user.has_perm("assets.approve_assetplanningrequest")

    @action(description=_("Approve"), icon="check_circle", permissions=["approve"])
    def approve_submit_action(self, request, obj):
        if obj.status != AssetPlanningRequestStatus.PENDING_REVIEW:
            return

        if obj.planning_type == PlanningType.NONE:
            self._approve_zone_sync_request(request, obj)
            return

        # transport_asset/assigned_to are filled later by auto-assignment.
        # Asset is only required for service types that list it in SERVICE_TYPE_FIELDS.
        missing = []
        required = SERVICE_TYPE_FIELDS.get(obj.service_type, set())
        if "asset" in required and not obj.asset_id:
            missing.append(_("Asset"))
        if "machine" in required and not obj.machine_id:
            missing.append(_("Machine"))
        if not obj.project_file_id:
            missing.append(_("Project File"))
        if missing:
            messages.error(
                request,
                _("These fields are required to approve: %(fields)s") % {"fields": ", ".join(str(m) for m in missing)},
            )
            return

        with transaction.atomic():
            previous_planning = obj.resulting_planning
            planning = AssetPlanning.objects.create(
                company=obj.company,
                asset=obj.asset,
                machine=obj.machine,
                origin=obj.from_zone,
                destination=obj.to_zone,
                assigned_to=obj.assigned_to,
                transport_asset=obj.transport_asset,
                project_file=obj.project_file,
                scheduled_date=obj.needed_date,
                scheduled_time=obj.needed_time,
                estimated_duration=obj.estimated_duration,
                urgency=obj.urgency,
                planning_type=obj.planning_type,
                service_type=obj.service_type,
                cuba_operation_type=obj.cuba_operation_type,
                cuba_size=obj.cuba_size,
                machinist_moved=obj.machinist_moved,
                load_description=obj.load_description,
                notes=obj.notes,
            )
            planning.suggested_trucks.set(obj.suggested_trucks.all())
            planning.moved_employees.set(obj.moved_employees.all())

            if previous_planning is not None:
                previous_planning.superseded_by = planning
                previous_planning.status = "cancelled"
                previous_planning.save(update_fields=["superseded_by", "status"])

            obj.status = AssetPlanningRequestStatus.APPROVED
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
            obj.resulting_planning = planning
            obj.save()

        if previous_planning is not None:
            messages.success(
                request,
                _("Request approved. New Asset Planning created; previous plan marked as old/cancelled."),
            )
        else:
            messages.success(request, _("Request approved. Asset Planning created."))

    def _approve_zone_sync_request(self, request, obj):
        """planning_type == NONE requests never get an AssetPlanning — just
        approve and sync asset/moved_employees to to_zone: immediately if due
        now, otherwise apply_due_zone_sync_requests() (Celery beat) picks it
        up once needed_date/needed_time arrives.
        """
        if not obj.asset_id and not obj.moved_employees.exists():
            messages.error(request, _("Select at least one machinist or an asset to approve this request."))
            return

        obj.status = AssetPlanningRequestStatus.APPROVED
        obj.reviewed_by = request.user
        obj.reviewed_at = timezone.now()
        obj.save()

        if obj.is_zone_sync_due:
            obj.apply_zone_change()
            messages.success(request, _("Request approved. Zone updated."))
        elif obj.needed_time and obj.needed_date == timezone.localdate():
            messages.success(
                request,
                _("Request approved. Zone will update at %(time)s today.") % {"time": obj.needed_time.strftime("%H:%M")},
            )
        else:
            messages.success(
                request,
                _("Request approved. Zone will update on %(date)s.") % {"date": obj.needed_date},
            )

    @action(description=_("Reject"), icon="cancel", variant=ActionVariant.DANGER, permissions=["approve"])
    def reject_submit_action(self, request, obj):
        if obj.status != AssetPlanningRequestStatus.PENDING_REVIEW:
            return

        obj.status = AssetPlanningRequestStatus.REJECTED
        obj.reviewed_by = request.user
        obj.reviewed_at = timezone.now()
        obj.save()
        messages.success(request, _("Request rejected."))

    @action(description=_("Duplicate"), url_path="duplicate", icon="content_copy")
    def duplicate_action(self, request, object_id):
        obj = self.get_object(request, object_id)
        if obj is None:
            return self._get_obj_does_not_exist_redirect(request, self.opts, object_id)

        trucks = list(obj.suggested_trucks.all())
        moved = list(obj.moved_employees.all())
        obj.pk = None
        obj._state.adding = True
        obj.status = AssetPlanningRequestStatus.PENDING_REVIEW
        obj.reviewed_by = None
        obj.reviewed_at = None
        obj.rejection_reason = ""
        obj.resulting_planning = None
        obj.zone_sync_applied_at = None
        obj.save()
        obj.suggested_trucks.set(trucks)
        obj.moved_employees.set(moved)

        messages.success(request, _("Request duplicated — review and save the copy."))
        return redirect(reverse("admin:assets_assetplanningrequest_change", args=[obj.pk]))
