from django.contrib import admin
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action, display

from core.admin.company_admin import CompanyRestrictedAdmin
from assets.models import AssetPlanningRequestDraft, AssetPlanningRequestDraftStatus


class NewClientRequestedFilter(admin.SimpleListFilter):
    title = _("New client requested")
    parameter_name = "new_client_requested"

    def lookups(self, request, model_admin):
        return [("yes", _("Yes")), ("no", _("No"))]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(client__isnull=True, new_client_name__gt="")
        if self.value() == "no":
            return queryset.exclude(client__isnull=True, new_client_name__gt="")
        return queryset


class NewZoneRequestedFilter(admin.SimpleListFilter):
    title = _("New zone requested")
    parameter_name = "new_zone_requested"

    def lookups(self, request, model_admin):
        return [("yes", _("Yes")), ("no", _("No"))]

    def _new_zone_q(self):
        return (
            Q(to_zone__isnull=True, destination_text__gt="")
            | Q(from_zone__isnull=True, origin_text__gt="")
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(self._new_zone_q())
        if self.value() == "no":
            return queryset.exclude(self._new_zone_q())
        return queryset


@admin.register(AssetPlanningRequestDraft)
class AssetPlanningRequestDraftAdmin(CompanyRestrictedAdmin, ModelAdmin):
    """
    Read-only mobile-app hints (see AssetPlanningRequestDraft docstring). A
    planner turns a SUBMITTED draft into a real AssetPlanningRequest with
    "Create real request", which prefills the add form
    (AssetPlanningRequestAdmin.get_changeform_initial_data) and, once that
    request is saved, marks this draft PROCESSED (AssetPlanningRequestAdmin.response_add).
    """
    list_display = (
        "service_type", "show_client", "show_destination", "needed_date", "show_status", "requested_by", "company",
    )
    list_filter = (NewClientRequestedFilter, NewZoneRequestedFilter, "status", "service_type", "company")
    search_fields = (
        "client__name", "new_client_name",
        "to_zone__name", "destination_text", "from_zone__name", "origin_text",
        "requested_by__first_name", "requested_by__last_name",
    )
    autocomplete_fields = ["company", "client", "to_zone", "from_zone", "asset", "machine", "resulting_request"]
    list_select_related = ["client", "to_zone", "requested_by", "company"]
    readonly_fields = [
        "service_type", "new_client_request_notice", "client",
        "new_client_name", "new_client_tax_id", "new_client_fiscal_address",
        "new_zone_request_notice",
        "to_zone", "destination_text", "destination_city",
        "from_zone", "origin_text", "origin_city",
        "needed_date", "needed_time", "urgency", "notes",
        "cuba_operation_type", "cuba_size", "asset", "machine", "load_description",
        "moved_employees",
        "requested_by", "status", "resulting_request",
    ]
    actions_row = ["create_request_action"]

    fieldsets = (
        (_("Request (written by the requester)"), {
            "fields": (
                "company", "service_type", "new_client_request_notice", "client",
                ("new_client_name", "new_client_tax_id"), "new_client_fiscal_address",
                "new_zone_request_notice",
                ("to_zone", "destination_text", "destination_city"),
                ("from_zone", "origin_text", "origin_city"),
                ("needed_date", "needed_time"), "urgency", "notes",
                ("cuba_operation_type", "cuba_size"),
                ("asset", "machine", "load_description"),
                "moved_employees",
            )
        }),
        (_("Status"), {"fields": ("status", "requested_by", "resulting_request")}),
    )

    @display(
        description=_("Status"),
        ordering="status",
        label={
            AssetPlanningRequestDraftStatus.DRAFT: "info",
            AssetPlanningRequestDraftStatus.SUBMITTED: "warning",
            AssetPlanningRequestDraftStatus.PROCESSED: "success",
            AssetPlanningRequestDraftStatus.CANCELLED: "danger",
        },
    )
    def show_status(self, obj):
        return obj.status, obj.get_status_display()

    @display(description=_("Client"), label={"new": "danger"})
    def show_client(self, obj):
        if obj.requests_new_client:
            label = obj.new_client_name
            if obj.new_client_tax_id:
                label += f" ({obj.new_client_tax_id})"
            return "new", label
        return obj.client.name if obj.client_id else "—"

    @display(description=_("Destination"), label={"new": "danger"})
    def show_destination(self, obj):
        if obj.requests_new_destination_zone:
            return "new", f"{obj.destination_text}, {obj.destination_city}"
        return obj.to_zone.name if obj.to_zone_id else "—"

    @display(description=_("New Client Request"))
    def new_client_request_notice(self, obj):
        if not obj or not obj.requests_new_client:
            return "—"
        return _(
            "⚠️ This requester couldn't find their client — a NEW client must be "
            "created first (suggested name/tax ID/fiscal address below) before this can become a real request."
        )

    @display(description=_("New Zone Request"))
    def new_zone_request_notice(self, obj):
        if not obj:
            return "—"
        parts = []
        if obj.requests_new_destination_zone:
            parts.append(
                _("Destination: %(street)s, %(city)s") % {
                    "street": obj.destination_text, "city": obj.destination_city,
                }
            )
        if obj.requests_new_origin_zone:
            parts.append(
                _("Origin: %(street)s, %(city)s") % {
                    "street": obj.origin_text, "city": obj.origin_city,
                }
            )
        if not parts:
            return "—"
        return _("⚠️ This requester couldn't find a working zone — a NEW one must be created first. ") + " / ".join(str(p) for p in parts)

    @action(description=_("Create real request"), url_path="create-request", icon="assignment_turned_in")
    def create_request_action(self, request, object_id):
        draft = self.get_object(request, object_id)
        if draft is None:
            return self._get_obj_does_not_exist_redirect(request, self.opts, object_id)
        if draft.status != AssetPlanningRequestDraftStatus.SUBMITTED:
            self.message_user(
                request,
                _("Only submitted drafts can be turned into a real request."),
                level="warning",
            )
            return HttpResponseRedirect(reverse("admin:assets_assetplanningrequestdraft_changelist"))
        return HttpResponseRedirect(
            reverse("admin:assets_assetplanningrequest_add") + f"?draft_id={object_id}"
        )

    def has_create_request_action_permission(self, request, object_id=None):
        return request.user.has_perm("assets.add_assetplanningrequest")
