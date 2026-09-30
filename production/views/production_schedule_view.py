"""The daily production planning calendar and its drag & drop action endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.urls import reverse
from django.utils import dateparse, timezone
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from production.constants import PlanningActions
from production.forms.production_schedule_filters import (
    GROUP_EMPLOYEE,
    GROUP_MACHINE,
    GROUP_MACHINE_PEOPLE,
    GROUP_TICKET,
    ProductionScheduleFilterForm,
)
from production.models import PlannedProductionSession, ProductionTicket
from production.services.planning_backlog import unplanned_tickets
from production.services.production_planning import ProductionPlanningService
from production.services.schedule_context import ProductionScheduleContext
from production.services.schedule_geometry import ScheduleWindow, fmt_pct
from production.services.working_calendar import WorkingCalendar

VALID_GROUPINGS = {
    GROUP_MACHINE_PEOPLE,
    GROUP_MACHINE,
    GROUP_EMPLOYEE,
    GROUP_TICKET,
}


def _resolve_company(request):
    companies = get_user_companies(request.user)
    if not companies.exists():
        raise PermissionDenied(_("You have no company assigned."))
    # A single-company scope is the common case; multi-company selection is
    # left to the existing switcher rather than reintroduced here.
    return companies.first()


class ProductionScheduleView(UnfoldModelAdminViewMixin, TemplateView):
    title = _("Production schedule")
    permission_required = ("production.view_plannedproductionsession",)
    template_name = "admin/production/plannedproductionsession/schedule.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = _resolve_company(self.request)

        params = self.request.GET.copy()
        # Keep old links useful: the previous UI used ?view=board|ticket.
        legacy_view = params.get("view")
        if "group_by" not in params and legacy_view:
            params["group_by"] = (
                GROUP_TICKET if legacy_view == "ticket" else GROUP_MACHINE_PEOPLE
            )
        if "group_by" not in params:
            params["group_by"] = GROUP_MACHINE_PEOPLE
        if "day" not in params:
            params["day"] = timezone.localdate().isoformat()

        form = ProductionScheduleFilterForm(params)
        if form.is_valid():
            day = form.cleaned_data["day"] or timezone.localdate()
            group_by = form.cleaned_data["group_by"]
        else:
            day = timezone.localdate()
            group_by = GROUP_MACHINE_PEOPLE
        if group_by not in VALID_GROUPINGS:
            group_by = GROUP_MACHINE_PEOPLE

        start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
        end = start + timedelta(days=1)

        data = ProductionScheduleContext(company=company, start=start, end=end).build()

        navigation_params = params.copy()
        navigation_params.pop("view", None)
        navigation_params["day"] = (day - timedelta(days=1)).isoformat()
        previous_day_url = reverse(
            "admin:production_plannedproductionsession_schedule",
            query=navigation_params,
        )
        navigation_params["day"] = (day + timedelta(days=1)).isoformat()
        next_day_url = reverse(
            "admin:production_plannedproductionsession_schedule",
            query=navigation_params,
        )
        rows = {
            GROUP_MACHINE: data.machine_rows,
            GROUP_EMPLOYEE: data.employee_rows,
            GROUP_TICKET: data.ticket_rows,
        }.get(group_by, [])

        backlog = unplanned_tickets(company)
        ctx.update(
            company=company,
            form=form,
            day=day,
            group_by=group_by,
            is_machine_people=group_by == GROUP_MACHINE_PEOPLE,
            is_machine=group_by == GROUP_MACHINE,
            is_employee=group_by == GROUP_EMPLOYEE,
            is_ticket=group_by == GROUP_TICKET,
            rows=rows,
            previous_day_url=previous_day_url,
            next_day_url=next_day_url,
            data=data,
            window_start_iso=start.isoformat(),
            window_end_iso=end.isoformat(),
            backlog=backlog,
            backlog_json=[item.as_dict() for item in backlog],
            action_url=self._action_url(),
            can_plan=self.request.user.has_perm("production.add_plannedproductionsession"),
            can_change=self.request.user.has_perm(
                "production.change_plannedproductionsession"
            ),
            can_unplan=self.request.user.has_perm(
                "production.delete_plannedproductionsession"
            ),
            snap_minutes=15,
            js_strings=self._js_strings(),
        )
        return ctx

    def _action_url(self) -> str:
        return reverse("admin:production_plannedproductionsession_schedule_action")

    @staticmethod
    def _js_strings() -> dict:
        """The handful of user-facing strings schedule.js needs to show.

        admin:jsi18n only serves django.contrib.admin's own catalogue, so JS
        can't gettext() its own strings; rendering them here reuses this
        view's translation pipeline instead.
        """
        return {
            "unplanTitle": _("Return to pending"),
            "unplanMessage": _(
                "It will be removed from the calendar and returned to the "
                "pending list. Its time and assigned resources will be lost."
            ),
            "missingInfoTitle": _("Missing information"),
            "missingDurationMessage": _(
                "Enter the stage's duration before dragging it."
            ),
            "networkErrorTitle": _("Network error"),
            "networkErrorMessage": _("Network error while saving the change."),
            "hasWarningsTitle": _("This has some warnings"),
            "couldNotApplyTitle": _("The change could not be applied"),
        }


class ProductionScheduleActionView(UnfoldModelAdminViewMixin, View):
    """JSON mutation endpoint the calendar's drag & drop posts to."""

    permission_required = ("production.view_plannedproductionsession",)

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse(
                {"ok": False, "error": _("Malformed request body.")}, status=400
            )

        self._window = self._build_window(request, payload)

        action = payload.get("action")
        handler = {
            PlanningActions.MOVE_SESSION: self._move_session,
            PlanningActions.RESIZE_SESSION: self._resize_session,
            PlanningActions.SET_RESOURCES: self._set_resources,
            PlanningActions.UNPLAN_SESSION: self._unplan_session,
            PlanningActions.PLAN_TICKET: self._plan_ticket,
            PlanningActions.PLAN_TICKET_CUSTOM: self._plan_ticket_custom,
            PlanningActions.PLAN_TICKET_STAGE_CUSTOM: self._plan_ticket_stage_custom,
            PlanningActions.REORDER_TICKET: self._reorder_ticket,
        }.get(action)

        if handler is None:
            return JsonResponse(
                {"ok": False, "error": _("Unknown action.")}, status=400
            )

        required_perm = self._required_permission(action)
        if required_perm and not request.user.has_perm(required_perm):
            return JsonResponse(
                {"ok": False, "error": _("Permission denied.")}, status=403
            )

        # ProductionPlanningService trusts a bare id, so this boundary must reject
        # another company's id first. "Not found" rather than 403: a company you
        # can't see should be indistinguishable from one that doesn't exist.
        try:
            if not self._targets_own_company(request, payload):
                return JsonResponse(
                    {"ok": False, "error": _("Not found.")}, status=404
                )
        except (TypeError, ValueError):
            return JsonResponse(
                {"ok": False, "error": _("Invalid identifier.")}, status=400
            )

        try:
            return handler(request, payload)
        except (KeyError, TypeError, ValueError) as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    @staticmethod
    def _targets_own_company(request, payload) -> bool:
        company = _resolve_company(request)
        session_id = payload.get("session_id")
        if session_id is not None and not PlannedProductionSession.objects.filter(
            pk=int(session_id), company=company
        ).exists():
            return False

        ticket_id = payload.get("ticket_id")
        if ticket_id is not None and not ProductionTicket.objects.filter(
            pk=int(ticket_id), company=company
        ).exists():
            return False

        return True

    @staticmethod
    def _required_permission(action: str) -> str | None:
        if action in {
            PlanningActions.PLAN_TICKET,
            PlanningActions.PLAN_TICKET_CUSTOM,
            PlanningActions.PLAN_TICKET_STAGE_CUSTOM,
        }:
            return "production.add_plannedproductionsession"
        if action == PlanningActions.UNPLAN_SESSION:
            return "production.delete_plannedproductionsession"
        return "production.change_plannedproductionsession"

    # --- payload parsing ---------------------------------------------------

    @staticmethod
    def _parse_datetime(value) -> datetime:
        parsed = dateparse.parse_datetime(value) if isinstance(value, str) else None
        if parsed is None:
            raise ValueError(_("Invalid date/time: %(value)s") % {"value": value})
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return parsed

    def _build_window(self, request, payload) -> ScheduleWindow | None:
        """The board's visible range, round-tripped in every payload since this
        endpoint has no view state of its own."""
        raw_start, raw_end = payload.get("window_start"), payload.get("window_end")
        if not raw_start or not raw_end:
            return None
        try:
            start = self._parse_datetime(raw_start)
            end = self._parse_datetime(raw_end)
        except ValueError:
            return None

        company = _resolve_company(request)
        calendar = WorkingCalendar.for_company(company)
        return ScheduleWindow.build(calendar, start, end)

    def _respond(self, result) -> JsonResponse:
        window = getattr(self, "_window", None)
        body = {
            "ok": result.ok,
            "sessions": [_session_json(s, window) for s in result.sessions],
            "conflicts": [c.as_dict() for c in result.conflicts],
            "requires_confirmation": result.requires_confirmation,
        }
        status = 200
        if not result.ok:
            status = 409 if result.blocked else 200
        return JsonResponse(body, status=status)

    # --- handlers ------------------------------------------------------

    def _move_session(self, request, payload):
        result = ProductionPlanningService.move_session(
            int(payload["session_id"]),
            new_start=self._parse_datetime(payload["start"]),
            duration_minutes=payload.get("duration_minutes"),
            resource_ids=payload.get("resource_ids"),
            override=bool(payload.get("override")),
        )
        return self._respond(result)

    def _resize_session(self, request, payload):
        new_start = payload.get("start")
        new_end = payload.get("end")
        result = ProductionPlanningService.resize_session(
            int(payload["session_id"]),
            new_start=self._parse_datetime(new_start) if new_start else None,
            new_end=self._parse_datetime(new_end) if new_end else None,
            override=bool(payload.get("override")),
        )
        return self._respond(result)

    def _set_resources(self, request, payload):
        result = ProductionPlanningService.set_resources(
            int(payload["session_id"]),
            resource_ids=payload.get("resource_ids", []),
            override=bool(payload.get("override")),
        )
        return self._respond(result)

    def _unplan_session(self, request, payload):
        result = ProductionPlanningService.unplan(
            int(payload["session_id"]), override=bool(payload.get("override"))
        )
        return self._respond(result)

    def _plan_ticket(self, request, payload):
        result = ProductionPlanningService.plan_ticket(
            int(payload["ticket_id"]),
            start_at=self._parse_datetime(payload["start"]),
            anchor_resource_id=payload.get("anchor_resource_id"),
            preferred_resource_ids=payload.get("preferred_resource_ids"),
            override=bool(payload.get("override")),
        )
        return self._respond(result)

    def _plan_ticket_custom(self, request, payload):
        result = ProductionPlanningService.plan_ticket_custom(
            int(payload["ticket_id"]),
            start_at=self._parse_datetime(payload["start"]),
            stages=payload["stages"],
            override=bool(payload.get("override")),
        )
        return self._respond(result)

    def _plan_ticket_stage_custom(self, request, payload):
        result = ProductionPlanningService.plan_ticket_stage_custom(
            int(payload["ticket_id"]),
            start_at=self._parse_datetime(payload["start"]),
            stage=payload["stage"],
            override=bool(payload.get("override")),
        )
        return self._respond(result)

    def _reorder_ticket(self, request, payload):
        result = ProductionPlanningService.reorder(
            int(payload["ticket_id"]),
            session_id=int(payload["session_id"]),
            before_session_id=(
                int(payload["before_session_id"])
                if payload.get("before_session_id") is not None
                else None
            ),
            after_session_id=(
                int(payload["after_session_id"])
                if payload.get("after_session_id") is not None
                else None
            ),
            cascade=bool(payload.get("cascade", True)),
            override=bool(payload.get("override")),
        )
        return self._respond(result)


def _session_json(session, window: ScheduleWindow | None) -> dict:
    data = {
        "id": session.pk,
        "ticket_id": session.production_ticket_id,
        "stage": session.production_stage,
        "sequence": session.sequence,
        "start": session.planned_start.isoformat(),
        "end": session.planned_end.isoformat(),
        "minutes": session.duration_minutes,
        "resource_ids": [
            assignment.resource_id for assignment in session.resources.all()
        ],
        "locked": session.locked,
    }
    if window is not None and not window.is_empty:
        left, width = window.block(session.planned_start, session.planned_end)
        data["left"] = fmt_pct(left)
        data["width"] = fmt_pct(width)
    return data
