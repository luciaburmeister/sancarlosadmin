"""The daily production planning calendar and its drag & drop action endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.urls import reverse
from django.utils import dateparse, timezone
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from company.models import Employee
from core.admin.company_access import get_user_companies
from production.constants import PlanningActions
from production.forms.production_schedule_filters import (
    GROUP_EMPLOYEE,
    GROUP_MACHINE,
    GROUP_MACHINE_PEOPLE,
    GROUP_TICKET,
    PERIOD_DAY,
    PERIOD_WEEK,
    ProductionScheduleFilterForm,
)
from production.models import PlannedProductionSession, ProductionTicket
from production.services.stage_booking_splitting import StageBookingSplittingService
from production.services.planning_backlog import unplanned_tickets
from production.services.production_planning import ProductionPlanningService
from production.services.schedule_context import ProductionScheduleContext
from production.services.schedule_geometry import ScheduleWindow, fmt_pct
from production.services.schedule_week import WeeklyScheduleContext
from production.services.working_calendar import WorkingCalendar
from production.services.stages_to_sessions import StagesToSessionsService

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
        if "period" not in params:
            params["period"] = PERIOD_DAY

        form = ProductionScheduleFilterForm(params)
        if form.is_valid():
            day = form.cleaned_data["day"] or timezone.localdate()
            group_by = form.cleaned_data["group_by"]
            period = form.cleaned_data["period"] or PERIOD_DAY
        else:
            day = timezone.localdate()
            group_by = GROUP_MACHINE_PEOPLE
            period = PERIOD_DAY
        if group_by not in VALID_GROUPINGS:
            group_by = GROUP_MACHINE_PEOPLE

        navigation_params = params.copy()
        navigation_params.pop("view", None)
        navigation_params.pop("expand_stage", None)
        navigation_params["period"] = period
        navigation_params["group_by"] = group_by
        step = timedelta(days=7 if period == PERIOD_WEEK else 1)
        navigation_params["day"] = (day - step).isoformat()
        previous_day_url = reverse(
            "admin:production_plannedproductionsession_schedule",
            query=navigation_params,
        )
        navigation_params["day"] = (day + step).isoformat()
        next_day_url = reverse(
            "admin:production_plannedproductionsession_schedule",
            query=navigation_params,
        )
        navigation_params["day"] = day.isoformat()
        navigation_params["period"] = PERIOD_DAY
        day_view_url = reverse(
            "admin:production_plannedproductionsession_schedule",
            query=navigation_params,
        )
        navigation_params["period"] = PERIOD_WEEK
        week_view_url = reverse(
            "admin:production_plannedproductionsession_schedule",
            query=navigation_params,
        )
        ctx.update(
            company=company,
            calendar_timezone=timezone.get_current_timezone_name(),
            action_url=self._action_url(),
            can_plan=self.request.user.has_perm("production.add_plannedproductionsession"),
            can_change=self.request.user.has_perm("production.change_plannedproductionsession"),
            can_unplan=self.request.user.has_perm("production.delete_plannedproductionsession"),
            can_complete_stage=(
                self.request.user.has_perm("production.change_productionitem")
                and Employee.can_reopen_production_stages(self.request.user, company.pk)
            ),
            snap_minutes=15,
            js_strings=self._js_strings(),
            form=form,
            day=day,
            group_by=group_by,
            period=period,
            is_week=period == PERIOD_WEEK,
            previous_day_url=previous_day_url,
            next_day_url=next_day_url,
            day_view_url=day_view_url,
            week_view_url=week_view_url,
        )
        if period == PERIOD_WEEK:
            week_start = day - timedelta(days=day.weekday())
            week = WeeklyScheduleContext(
                company=company, week_start=week_start, group_by=group_by
            ).build()
            for week_day in week["days"]:
                navigation_params["period"] = PERIOD_DAY
                navigation_params["day"] = week_day["date"].isoformat()
                week_day["url"] = reverse(
                    "admin:production_plannedproductionsession_schedule",
                    query=navigation_params,
                )
            for row in week["rows"]:
                entry_params = (
                    navigation_params.copy()
                    if group_by == GROUP_MACHINE_PEOPLE else None
                )
                if entry_params is not None:
                    entry_params["period"] = PERIOD_DAY
                    entry_params["expand_stage"] = row["label"]
                for week_day, cell in zip(week["days"], row["cells"]):
                    cell["date"] = week_day["date"]
                    cell["url"] = week_day["url"]
                    if entry_params is not None:
                        entry_params["day"] = week_day["date"].isoformat()
                        cell["url"] = reverse(
                            "admin:production_plannedproductionsession_schedule",
                            query=entry_params,
                        )
            ctx.update(
                week=week,
                week_start=week_start,
                week_end=week_start + timedelta(days=6),
            )
            return ctx

        start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
        end = start + timedelta(days=1)
        data = ProductionScheduleContext(company=company, start=start, end=end).build()
        initial_group_key = None
        if group_by == GROUP_MACHINE_PEOPLE:
            requested_stage = self.request.GET.get("expand_stage")
            initial_group_key = next(
                (
                    group.stage
                    for group in data.stage_groups
                    if group.stage == requested_stage
                ),
                None,
            )
        rows = {
            GROUP_MACHINE: data.machine_rows,
            GROUP_EMPLOYEE: data.employee_rows,
            GROUP_TICKET: data.ticket_rows,
        }.get(group_by, [])

        backlog = unplanned_tickets(company)
        ctx.update(
            is_machine_people=group_by == GROUP_MACHINE_PEOPLE,
            is_machine=group_by == GROUP_MACHINE,
            is_employee=group_by == GROUP_EMPLOYEE,
            is_ticket=group_by == GROUP_TICKET,
            initial_group_key=initial_group_key,
            rows=rows,
            data=data,
            window_start_iso=start.isoformat(),
            window_end_iso=end.isoformat(),
            backlog=backlog,
            backlog_json=[item.as_dict() for item in backlog],
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
                "This booking will be removed from the calendar. Its time and assigned "
                "resources will be lost. Other bookings for the stage will stay."
            ),
            "missingInfoTitle": _("Missing information"),
            "missingDurationMessage": _(
                "Enter the stage's duration before dragging it."
            ),
            "networkErrorTitle": _("Network error"),
            "networkErrorMessage": _("Network error while saving the change."),
            "hasWarningsTitle": _("This has some warnings"),
            "overflowTitle": _("Move the remaining work to the next available day?"),
            "overflowMessage": _("This booking exceeds the day's working hours. Confirm these bookings to keep all planned working time."),
            "overflowConfirm": _("Confirm bookings"),
            "overflowPart": _("%(start)s – %(end)s · %(minutes)s min"),
            "couldNotApplyTitle": _("The change could not be applied"),
            "cutMinutes": _("%(first)s min · %(second)s min"),
            "invalidCut": _("Choose a cut inside the booking, with at least one working minute on each side."),
            "completeStageTitle": _("Mark as done"),
            "completeStageMessage": _("Mark %(stage)s as done for every item in this ticket? No additional working time will be recorded."),
        }


class ProductionScheduleActionView(UnfoldModelAdminViewMixin, View):
    """JSON mutation endpoint the calendar's drag & drop posts to."""

    permission_required = ("production.view_plannedproductionsession",)

    def get(self, request, *args, **kwargs):
        try:
            session_id = int(request.GET.get("session_id", ""))
        except ValueError:
            return JsonResponse({"ok": False, "error": _("Invalid identifier.")}, status=400)
        session = PlannedProductionSession.objects.select_related("company").filter(
            pk=session_id, company=_resolve_company(request),
        ).first()
        if session is None:
            return JsonResponse({"ok": False, "error": _("Not found.")}, status=404)
        calendar = WorkingCalendar.for_company(session.company)
        windows = calendar.windows_between(session.planned_start, session.planned_end)
        if calendar.has_slots:
            working_windows = [{"start": w.start.isoformat(), "end": w.end.isoformat(), "minutes": w.minutes} for w in windows]
        else:
            working_windows = [{"start": session.planned_start.isoformat(), "end": session.planned_end.isoformat(), "minutes": session.duration_minutes}]
        return JsonResponse({
            "ok": True, "session": _session_json(session, None),
            "working_minutes": StageBookingSplittingService.working_minutes(session, calendar),
            "working_windows": working_windows,
        })

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse(
                {"ok": False, "error": _("Malformed request body.")}, status=400
            )
        if not isinstance(payload, dict):
            return JsonResponse({"ok": False, "error": _("Malformed request body.")}, status=400)

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
            PlanningActions.SPLIT_BOOKING: self._split_booking,
            PlanningActions.MOVE_SESSION_DAY: self._move_session_day,
            PlanningActions.COMPLETE_STAGE: self._complete_stage,
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
        if (action == PlanningActions.SPLIT_BOOKING or "overflow_plan" in payload) and not request.user.has_perm(
            "production.add_plannedproductionsession"
        ):
            return JsonResponse({"ok": False, "error": _("Permission denied.")}, status=403)

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
        except PermissionDenied as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=403)
        except ValidationError as exc:
            return JsonResponse({"ok": False, "error": " ".join(exc.messages)}, status=400)
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
        if action == PlanningActions.COMPLETE_STAGE:
            return "production.change_productionitem"
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
            "overflow_plan": result.overflow_plan,
        }
        status = 200
        if not result.ok:
            status = 409 if result.blocked else 200
        return JsonResponse(body, status=status)

    # --- handlers ------------------------------------------------------

    def _complete_stage(self, request, payload):
        company = _resolve_company(request)
        if not Employee.can_reopen_production_stages(request.user, company.pk):
            raise PermissionDenied(_("Only production supervisors can mark stages as done without planning."))
        employee = Employee.objects.filter(user=request.user, company=company, is_active=True).first()
        ticket = ProductionTicket.objects.filter(pk=int(payload["ticket_id"]), company=company).first()
        if ticket is None:
            return JsonResponse({"ok": False, "error": _("Not found.")}, status=404)
        stage_name = payload["stage"]
        if not isinstance(stage_name, str) or not stage_name.strip():
            raise ValidationError(_("Choose a valid production stage."))
        count = StagesToSessionsService.complete_ticket_stage(
            ticket=ticket, stage_name=stage_name, employee=employee, user=request.user,
        )
        return JsonResponse({"ok": True, "completed_items": count})

    def _split_booking(self, request, payload):
        return self._respond(StageBookingSplittingService.split_booking(
            int(payload["session_id"]), cut_at=self._parse_datetime(payload["cut_at"]),
            expected_start=self._parse_datetime(payload["expected_start"]),
            expected_end=self._parse_datetime(payload["expected_end"]),
            override=bool(payload.get("override")),
        ))

    def _move_session_day(self, request, payload):
        day = dateparse.parse_date(payload["day"])
        if day is None:
            raise ValueError(_("Invalid date."))
        return self._respond(ProductionPlanningService.move_session_day(
            int(payload["session_id"]), day=day,
            expected_start=self._parse_datetime(payload["expected_start"]),
            expected_end=self._parse_datetime(payload["expected_end"]),
            override=bool(payload.get("override")),
            timetable=({"overflow_plan": payload.get("overflow_plan")}
                       if request.user.has_perm("production.add_plannedproductionsession") else None),
        ))

    def _move_session(self, request, payload):
        result = ProductionPlanningService.move_session(
            int(payload["session_id"]),
            new_start=self._parse_datetime(payload["start"]),
            duration_minutes=payload.get("duration_minutes"),
            resource_ids=payload.get("resource_ids"),
            override=bool(payload.get("override")),
            timetable=({"overflow_plan": payload.get("overflow_plan")}
                       if request.user.has_perm("production.add_plannedproductionsession") else None),
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
            assign_machines=True,
            override=bool(payload.get("override")),
            timetable={"overflow_plan": payload.get("overflow_plan")},
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
