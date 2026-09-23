"""Weekly drag-and-drop scheduling board (shared production terminal, supervisor mode)."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from api._utils import SharedTerminalAPIView, invalid_data_response, parse_date_or_error
from api.production.serializers import (
    CreateSessionFromBacklogSerializer,
    ReorderDaySessionsSerializer,
    RescheduleSessionSerializer,
    SupervisorPermissionSerializer,
)
from company.models import Employee, WeekdayChoices
from configurator.models import Configuration
from core.dates import week_bounds
from production.exceptions import PlannedSessionConflictError
from production.models import PlannedProductionSession, ProductionTicket, ProductionTicketStatus
from production.services import planning_board
from production.services.board_activity import build_week_activity
from production.services.session_scheduler import get_or_create_resource_for_target
from production.services.ticket_labels import build_ticket_labels


def _week_bounds(week_start_param):
    """Monday..Sunday for the week containing *week_start_param* (or today); (None, None, error) if unparseable."""
    requested, err = parse_date_or_error(week_start_param)
    if err:
        return None, None, err
    monday, sunday = week_bounds(requested or timezone.localdate())
    return monday, sunday, None


class ScheduleBoardView(SharedTerminalAPIView):
    def get(self, request):
        monday, sunday, err = _week_bounds(request.GET.get("week_start"))
        if err:
            return err

        employee_ct = ContentType.objects.get_for_model(Employee)

        days = [
            {
                "date": (monday + timedelta(days=i)).isoformat(),
                "weekday": i,
                "label": str(WeekdayChoices(i).label),
                "work_windows": [
                    {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
                    for start, end in self.company.working_windows_for_day(
                        monday + timedelta(days=i)
                    )
                ],
            }
            for i in range(7)
        ]

        employees = list(
            Employee.objects.filter(
                company=self.company, can_work_on_production=True, is_active=True,
            ).order_by("last_name", "first_name")
        )

        range_start = timezone.make_aware(datetime.combine(monday, time.min))
        range_end = timezone.make_aware(datetime.combine(sunday + timedelta(days=1), time.min))

        sessions_qs = (
            PlannedProductionSession.objects.filter(
                planned_start__lt=range_end,
                planned_end__gt=range_start,
                primary_resource__company=self.company,
                primary_resource__content_type=employee_ct,
            )
            .select_related("production_ticket__project_file__client")
            .prefetch_related(GenericPrefetch("primary_resource__target", [Employee.objects.all()]))
        )
        sessions = list(sessions_qs)

        ticket_ids = {s.production_ticket_id for s in sessions if s.production_ticket_id}
        tickets_by_id = {
            t.id: t
            for t in ProductionTicket.objects.filter(id__in=ticket_ids).prefetch_related(
                "items",
                GenericPrefetch(
                    "source_object", [Configuration.objects.select_related("product", "quote")]
                ),
            )
        }

        sessions_payload = []
        for s in sessions:
            employee = s.primary_resource.target if s.primary_resource_id else None
            ticket = tickets_by_id.get(s.production_ticket_id)
            labels = None
            if ticket is not None:
                labels = build_ticket_labels(
                    ticket=ticket, config=ticket.source_object, project_file=ticket.resolved_project_file,
                )
            entry = {
                "id": s.id,
                "production_ticket_id": s.production_ticket_id,
                "production_stage": s.production_stage,
                "production_item_ids": ticket.item_ids_for_stage(s.production_stage) if ticket else [],
                "planned_start": s.planned_start.isoformat(),
                "planned_end": s.planned_end.isoformat(),
                "employee_id": employee.id if employee else None,
                "employee_name": employee.full_name if employee else None,
                "locked": s.locked,
                "source": s.source,
                "notes": s.notes,
                "stage_completed": (
                    ticket is not None and not ticket.has_incomplete_stage(s.production_stage)
                ),
            }
            if labels is not None:
                entry.update({
                    "config_id": labels.config_id,
                    "config_name": labels.config_name,
                    "config_notes": labels.config_notes,
                    "quote_name": labels.quote_name,
                    "template_name": labels.template_name,
                    "ticket_status": labels.ticket_status,
                    "config_created_at": labels.config_created_at,
                    "expediente": labels.expediente,
                })
            sessions_payload.append(entry)

        unfinished_tickets = list(
            ProductionTicket.objects.filter(company=self.company)
            .exclude(status__in=[ProductionTicketStatus.DONE, ProductionTicketStatus.CANCELLED])
            .select_related("project_file__client")
            .prefetch_related(
                "items",
                GenericPrefetch(
                    "source_object", [Configuration.objects.select_related("product", "quote")]
                ),
            )
        )
        scheduled_stage_keys = set(
            PlannedProductionSession.objects.filter(
                production_ticket_id__in=[t.id for t in unfinished_tickets]
            ).values_list("production_ticket_id", "production_stage")
        )

        unfinished_configs = []
        backlog = []
        for ticket in unfinished_tickets:
            config = ticket.source_object
            if config is None:
                continue

            # ticket.status can lag behind reality; per-item stage completion is the reliable signal.
            incomplete_stages = [
                stage for stage in ticket.get_stage_order() if ticket.has_incomplete_stage(stage)
            ]
            if not incomplete_stages:
                continue

            labels = build_ticket_labels(
                ticket=ticket, config=config, project_file=ticket.resolved_project_file,
            )
            unfinished_configs.append({
                "ticket_id": ticket.id,
                "config_id": labels.config_id,
                "config_name": labels.config_name,
                "config_notes": labels.config_notes,
                "quote_name": labels.quote_name,
                "template_name": labels.template_name,
                "ticket_status": labels.ticket_status,
                "expediente": labels.expediente,
            })

            # Already-started stages belong in `activity`, not backlog.
            unstarted_stages = [s for s in incomplete_stages if ticket.is_stage_unstarted(s)]
            # Nothing left to assign if every stage is already scheduled.
            has_unscheduled = any(
                (ticket.id, s) not in scheduled_stage_keys for s in unstarted_stages
            )
            if not has_unscheduled:
                continue

            for stage in unstarted_stages:
                backlog.append({
                    "ticket_id": ticket.id,
                    "production_stage": stage,
                    "production_item_ids": ticket.item_ids_for_stage(stage),
                    "config_id": labels.config_id,
                    "config_name": labels.config_name,
                    "config_notes": labels.config_notes,
                    "quote_name": labels.quote_name,
                    "expediente": labels.expediente,
                    "is_scheduled": (ticket.id, stage) in scheduled_stage_keys,
                })

        activity = build_week_activity(self.company, range_start, range_end)

        return JsonResponse({
            "week_start": monday.isoformat(),
            "week_end": sunday.isoformat(),
            "prev_week_start": (monday - timedelta(days=7)).isoformat(),
            "next_week_start": (monday + timedelta(days=7)).isoformat(),
            "days": days,
            "employees": [{"id": e.id, "full_name": e.full_name} for e in employees],
            "unfinished_configurations": unfinished_configs,
            "sessions": sessions_payload,
            "backlog": backlog,
            "activity": activity,
        })


class RescheduleSessionView(SharedTerminalAPIView):
    def post(self, request, session_id):
        serializer = RescheduleSessionSerializer(
            data=request.data, context={"company": self.company}
        )
        if not serializer.is_valid():
            return invalid_data_response(serializer)
        data = serializer.validated_data

        # primary_resource__company scoping is load-bearing: prevents cross-company access by id.
        session = get_object_or_404(
            PlannedProductionSession, id=session_id, primary_resource__company=self.company,
        )

        employee = None
        if data.get("employee_id") is not None:
            employee = get_object_or_404(
                Employee, id=data["employee_id"], company=self.company, is_active=True,
            )

        try:
            updated = planning_board.move_session(
                company=self.company,
                session=session,
                employee=employee,
                day=data["date"],
                start_time=data.get("start_time"),
                end_time=data.get("end_time"),
                end_day=data.get("end_date"),
            )
        except PlannedSessionConflictError as e:
            return JsonResponse(
                {"error": e.message, "code": e.code, "conflicting_session_id": e.conflicting_session_id},
                status=409,
            )
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({
            "success": True,
            "session": {
                "id": updated.id,
                "planned_start": updated.planned_start.isoformat(),
                "planned_end": updated.planned_end.isoformat(),
                "employee_id": updated.primary_resource.object_id,
            },
        })


class DeleteSessionView(SharedTerminalAPIView):
    """Unassign a planned session — deleting it returns its ticket+stage to the backlog."""

    def post(self, request, session_id):
        serializer = SupervisorPermissionSerializer(
            data=request.data, context={"company": self.company}
        )
        if not serializer.is_valid():
            return invalid_data_response(serializer)

        # primary_resource__company scoping is load-bearing: prevents cross-company access by id.
        session = get_object_or_404(
            PlannedProductionSession, id=session_id, primary_resource__company=self.company,
        )
        try:
            session.unassign()
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({"success": True})


class CreateSessionFromBacklogView(SharedTerminalAPIView):
    def post(self, request):
        serializer = CreateSessionFromBacklogSerializer(
            data=request.data, context={"company": self.company}
        )
        if not serializer.is_valid():
            return invalid_data_response(serializer)
        data = serializer.validated_data

        ticket = get_object_or_404(
            ProductionTicket, id=data["production_ticket_id"], company=self.company,
        )
        employee = get_object_or_404(
            Employee, id=data["employee_id"], company=self.company, is_active=True,
        )

        try:
            session = planning_board.create_session(
                company=self.company,
                ticket=ticket,
                stage=data["production_stage"],
                employee=employee,
                day=data["date"],
                start_time=data.get("start_time"),
                end_time=data.get("end_time"),
                end_day=data.get("end_date"),
            )
        except PlannedSessionConflictError as e:
            return JsonResponse(
                {"error": e.message, "code": e.code, "conflicting_session_id": e.conflicting_session_id},
                status=409,
            )
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({
            "success": True,
            "session": {
                "id": session.id,
                "production_ticket_id": session.production_ticket_id,
                "production_stage": session.production_stage,
                "planned_start": session.planned_start.isoformat(),
                "planned_end": session.planned_end.isoformat(),
                "employee_id": employee.id,
            },
        }, status=201)


class ReorderDaySessionsView(SharedTerminalAPIView):
    """Drag-to-reorder one employee's planned sessions within a day; times are repacked to match."""

    def post(self, request):
        serializer = ReorderDaySessionsSerializer(
            data=request.data, context={"company": self.company}
        )
        if not serializer.is_valid():
            return invalid_data_response(serializer)
        data = serializer.validated_data

        employee = get_object_or_404(
            Employee, id=data["employee_id"], company=self.company, is_active=True,
        )
        resource = get_or_create_resource_for_target(employee)

        try:
            updated = planning_board.reorder_day_sessions(
                company=self.company,
                resource=resource,
                day=data["date"],
                session_ids=data["session_ids"],
            )
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({
            "success": True,
            "sessions": [
                {
                    "id": s.id,
                    "planned_start": s.planned_start.isoformat(),
                    "planned_end": s.planned_end.isoformat(),
                }
                for s in updated
            ],
        })
