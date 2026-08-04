from django.contrib import admin, messages
from django.db import transaction
from django.urls import reverse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from unfold.admin import ModelAdmin
from unfold.decorators import action, display
from unfold.enums import ActionVariant
from simple_history.admin import SimpleHistoryAdmin

from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.company_access import get_user_companies
from company.models import Employee
from assets.constants import SERVICE_TYPE_FIELDS, SERVICE_TYPE_MAQUINA_PROPIA
from assets.forms import StaticSelect2MultipleWidget
from assets.models import Asset, AssetPlanning, AssetPlanningRequest, AssetPlanningRequestStatus, AssetTypeCategory

# Built from SERVICE_TYPE_FIELDS so the form and AssetPlanningRequest.clean()
# never disagree about which fields apply to which service type.
_CONDITIONAL_FIELD_NAMES = ["from_zone", "asset", "load_description", "cuba_operation_type", "cuba_size"]
CONDITIONAL_FIELDS = {
    field_name: " || ".join(
        f"service_type == '{service_type}'"
        for service_type, fields in SERVICE_TYPE_FIELDS.items()
        if field_name in fields
    )
    for field_name in _CONDITIONAL_FIELD_NAMES
}
# machinist_moved isn't in SERVICE_TYPE_FIELDS (it's optional, never required)
# so it's wired up separately; moved_employees only makes sense once both
# machinist_moved is checked AND the service type is Own Machine Transport.
CONDITIONAL_FIELDS["machinist_moved"] = f"service_type == '{SERVICE_TYPE_MAQUINA_PROPIA}'"
CONDITIONAL_FIELDS["moved_employees"] = f"service_type == '{SERVICE_TYPE_MAQUINA_PROPIA}' && machinist_moved"


@admin.register(AssetPlanningRequest)
class AssetPlanningRequestAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin):
    list_display = (
        "service_type", "to_zone", "from_zone",
        "needed_date", "needed_time", "urgency", "show_status",
        "requested_by", "assigned_to", "asset", "company",
    )
    list_filter = ("status", "urgency", "service_type", "company")
    search_fields = (
        "to_zone__name", "from_zone__name",
        "requested_by__first_name", "requested_by__last_name",
        "assigned_to__first_name", "assigned_to__last_name",
        "asset__name",
    )
    autocomplete_fields = ["to_zone", "from_zone", "project_file", "suggested_trucks"]
    readonly_fields = [
        "planning_type", "requested_by", "status",
        "reviewed_by", "reviewed_at",
        "resulting_planning",
    ]
    actions_row = ["duplicate_action"]
    actions_submit_line = ["approve_submit_action", "reject_submit_action"]
    conditional_fields = CONDITIONAL_FIELDS

    class Media(StaticSelect2MultipleWidget.Media):
        pass

    fieldsets = (
        (_("Request"), {
            "fields": (
                "service_type", "planning_type",
                ("to_zone", "from_zone"),
                ("needed_date", "end_time"),
                "urgency",
                ("cuba_operation_type", "cuba_size"),
                ("machinist_moved", "moved_employees"),
                "load_description", "notes",
                ("asset", "assigned_to"),
                ("suggested_trucks", "transport_asset"),
            )
        }),
        (_("Review"), {
            "fields": (
                "status", "requested_by",
                ("reviewed_by", "reviewed_at"),
                "rejection_reason",
                "resulting_planning",
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

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Superusers pick company explicitly (see get_fieldsets), so their
        # pickers aren't pre-restricted to a single company; clean() still
        # rejects mismatched combinations on save.
        company = None if request.user.is_superuser else self._target_company(request)
        if db_field.name == "asset":
            qs = Asset.objects.filter(asset_type__category=AssetTypeCategory.MACHINE, is_active=True)
            if company:
                qs = qs.filter(company=company)
            kwargs["queryset"] = qs.order_by("name")
        elif db_field.name == "assigned_to":
            qs = Employee.objects.all()
            if company:
                qs = qs.filter(company=company)
            kwargs["queryset"] = qs.order_by("first_name", "last_name")
        elif db_field.name == "transport_asset":
            qs = Asset.objects.filter(asset_type__category=AssetTypeCategory.TRUCK, is_active=True)
            if company:
                qs = qs.filter(company=company)
            obj = getattr(request, "_obj_", None)
            if obj is not None and obj.pk:
                suggested_ids = obj.suggested_trucks.values_list("pk", flat=True)
                if suggested_ids:
                    qs = qs.filter(pk__in=suggested_ids)
            kwargs["queryset"] = qs.order_by("name")
        elif db_field.name == "project_file":
            from management.models import ProjectFile
            qs = ProjectFile.objects.all()
            if company:
                qs = qs.filter(company=company)
            kwargs["queryset"] = qs.order_by("reference", "name")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        company = None if request.user.is_superuser else self._target_company(request)
        if db_field.name == "suggested_trucks":
            qs = Asset.objects.filter(asset_type__category=AssetTypeCategory.TRUCK, is_active=True)
            if company:
                qs = qs.filter(company=company)
            kwargs["queryset"] = qs.order_by("name")
        elif db_field.name == "moved_employees":
            qs = Employee.objects.filter(is_active=True)
            if company:
                qs = qs.filter(company=company)
            kwargs["queryset"] = qs.order_by("first_name", "last_name")
            kwargs["widget"] = StaticSelect2MultipleWidget()
        form_field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if db_field.name == "moved_employees" and form_field is not None:
            widget = StaticSelect2MultipleWidget()
            if isinstance(form_field.widget, RelatedFieldWidgetWrapper):
                form_field.widget.widget = widget
            else:
                form_field.widget = widget
            form_field.help_text = ""
        return form_field

    @display(
        description=_("Status"),
        ordering="status",
        label={
            AssetPlanningRequestStatus.PENDING_REVIEW.label: "warning",
            AssetPlanningRequestStatus.APPROVED.label: "success",
            AssetPlanningRequestStatus.REJECTED.label: "danger",
        },
    )
    def show_status(self, obj):
        return obj.get_status_display()

    def has_approve_permission(self, request, object_id=None):
        return request.user.has_perm("assets.approve_assetplanningrequest")

    @action(description=_("Approve"), icon="check_circle", permissions=["approve"])
    def approve_submit_action(self, request, obj):
        if obj.status != AssetPlanningRequestStatus.PENDING_REVIEW:
            return

        # transport_asset/assigned_to are filled later by auto-assignment.
        # Asset is only required for service types that list it in SERVICE_TYPE_FIELDS.
        missing = []
        required = SERVICE_TYPE_FIELDS.get(obj.service_type, set())
        if "asset" in required and not obj.asset_id:
            missing.append(_("Asset"))
        if not obj.project_file_id:
            missing.append(_("Project File"))
        if missing:
            messages.error(
                request,
                _("These fields are required to approve: %(fields)s") % {"fields": ", ".join(str(m) for m in missing)},
            )
            return

        with transaction.atomic():
            planning = AssetPlanning.objects.create(
                company=obj.company,
                asset=obj.asset,
                origin=obj.from_zone,
                destination=obj.to_zone,
                assigned_to=obj.assigned_to,
                transport_asset=obj.transport_asset,
                project_file=obj.project_file,
                scheduled_date=obj.needed_date,
                scheduled_time=obj.needed_time,
                scheduled_end_time=obj.end_time,
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

            obj.status = AssetPlanningRequestStatus.APPROVED
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
            obj.resulting_planning = planning
            obj.save()

        messages.success(request, _("Request approved. Asset Planning created."))

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
        obj.save()
        obj.suggested_trucks.set(trucks)
        obj.moved_employees.set(moved)

        messages.success(request, _("Request duplicated — review and save the copy."))
        return redirect(reverse("admin:assets_assetplanningrequest_change", args=[obj.pk]))
