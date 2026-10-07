"""Read-only weekly overview of planned production sessions."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.db.models import Prefetch
from django.utils import timezone

from company.models import Employee, Machine
from production.forms.production_schedule_filters import (
    GROUP_EMPLOYEE,
    GROUP_MACHINE,
    GROUP_TICKET,
)
from production.models import (
    PlannedProductionSession,
    PlannedSessionResource,
    ProductionResource,
)
from production.services.schedule_context import nfc, ticket_color
from production.services.working_calendar import WorkingCalendar


class WeeklyScheduleContext:
    """Load one week's sessions once and group them into compact day cells."""

    def __init__(self, *, company, week_start: date, group_by: str):
        self.company = company
        self.week_start = week_start
        self.group_by = group_by

    def build(self) -> dict:
        calendar = WorkingCalendar.for_company(self.company)
        tz = calendar.timezone
        week_dates = [self.week_start + timedelta(days=offset) for offset in range(7)]
        dates = [day for day in week_dates if calendar.windows_for_date(day)]
        if not dates:
            return {"days": [], "rows": [], "has_schedule": calendar.has_slots}
        boundaries = [
            (
                datetime.combine(day, time.min, tzinfo=tz),
                datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz),
            )
            for day in dates
        ]
        sessions = list(
            PlannedProductionSession.objects.filter(
                company=self.company,
                planned_start__lt=datetime.combine(
                    week_dates[-1] + timedelta(days=1), time.min, tzinfo=tz
                ),
                planned_end__gt=datetime.combine(week_dates[0], time.min, tzinfo=tz),
            )
            .only(
                "id", "production_ticket_id", "production_stage",
                "planned_start", "planned_end",
            )
            .prefetch_related(
                Prefetch(
                    "resources",
                    queryset=PlannedSessionResource.objects.only(
                        "planned_session_id", "resource_id"
                    ),
                )
            )
            .order_by("planned_start", "id")
        )
        assigned_ids = {
            assignment.resource_id
            for session in sessions
            for assignment in session.resources.all()
        }
        resources = {
            resource.pk: resource
            for resource in ProductionResource.objects.filter(
                company=self.company, is_active=True, pk__in=assigned_ids
            ).prefetch_related(
                GenericPrefetch("target", [Employee.objects.all(), Machine.objects.all()])
            )
        }
        configured_stages = list(
            dict.fromkeys(
                stage
                for stage in (
                    nfc(value) for value in (self.company.production_stages or [])
                )
                if stage
            )
        )
        configured_stage_set = set(configured_stages)
        rows = {}
        for session in sessions:
            if (
                self.group_by not in (GROUP_MACHINE, GROUP_EMPLOYEE, GROUP_TICKET)
                and nfc(session.production_stage) not in configured_stage_set
            ):
                continue
            daily_entries = []
            for index, (day_start, day_end) in enumerate(boundaries):
                start = max(session.planned_start, day_start)
                end = min(session.planned_end, day_end)
                if start >= end:
                    continue
                start_label = timezone.localtime(start, tz).strftime("%H:%M")
                end_label = (
                    "24:00"
                    if end == day_end
                    else timezone.localtime(end, tz).strftime("%H:%M")
                )
                daily_entries.append((index, f"{start_label}–{end_label}"))
            if not daily_entries:
                continue
            assigned = [
                resources[assignment.resource_id]
                for assignment in session.resources.all()
                if assignment.resource_id in resources
                and resources[assignment.resource_id].target is not None
            ]
            machines = [
                resource for resource in assigned
                if isinstance(resource.target, Machine)
            ]
            operators = [
                resource for resource in assigned
                if isinstance(resource.target, Employee)
            ]
            ticket_label = (
                f"OP-{session.production_ticket_id}"
                if session.production_ticket_id is not None
                else "OP-?"
            )
            if self.group_by == GROUP_TICKET:
                entry_label = session.production_stage
            elif self.group_by in (GROUP_MACHINE, GROUP_EMPLOYEE):
                entry_label = f"{ticket_label} · {session.production_stage}"
            else:
                entry_label = ticket_label
            for key, label, detail in self._row_assignments(
                session, assigned, machines, operators
            ):
                row = rows.setdefault(
                    key,
                    {"label": label, "cells": [[] for _ in dates]},
                )
                for index, time_label in daily_entries:
                    row["cells"][index].append(
                        {
                            "time": time_label,
                            "label": entry_label,
                            "detail": detail,
                            "color": ticket_color(session.production_ticket_id),
                        }
                    )

        for row in rows.values():
            row["cells"] = [
                {"entries": entries[:3], "overflow_count": max(0, len(entries) - 3)}
                for entries in row["cells"]
            ]
        stage_order = {stage: index for index, stage in enumerate(configured_stages)}
        ordered_rows = sorted(
            rows.values(),
            key=(
                (lambda row: (stage_order.get(row["label"], len(stage_order)), row["label"]))
                if self.group_by not in (GROUP_MACHINE, GROUP_EMPLOYEE, GROUP_TICKET)
                else (lambda row: row["label"].casefold())
            ),
        )
        return {
            "days": [
                {
                    "date": day,
                    "is_today": day == timezone.localdate(),
                }
                for day in dates
            ],
            "rows": ordered_rows,
            "has_schedule": calendar.has_slots,
        }

    def _row_assignments(self, session, assigned, machines, operators):
        if self.group_by == GROUP_MACHINE:
            operator_names = ", ".join(str(resource.target) for resource in operators)
            return [
                (f"machine-{resource.pk}", str(resource.target), operator_names)
                for resource in machines
            ]
        if self.group_by == GROUP_EMPLOYEE:
            machine_names = ", ".join(str(resource.target) for resource in machines)
            return [
                (f"operator-{resource.pk}", str(resource.target), machine_names)
                for resource in operators
            ]
        if self.group_by == GROUP_TICKET:
            if session.production_ticket_id is None:
                return []
            return [
                (
                    f"ticket-{session.production_ticket_id}",
                    f"OP-{session.production_ticket_id}",
                    ", ".join(str(resource.target) for resource in assigned),
                )
            ]
        stage = nfc(session.production_stage)
        return [
            (
                f"stage-{stage}",
                stage,
                ", ".join(str(resource.target) for resource in assigned),
            )
        ]
