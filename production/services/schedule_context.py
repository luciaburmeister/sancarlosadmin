"""Build the daily production calendar's rows and blocks from planned sessions."""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime, timedelta

from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from company.models import Employee, Machine
from configurator.models import Configuration
from production.constants import TICKET_PALETTE
from production.models import (
    PlannedProductionSession,
    ProductionItem,
    ProductionItemStatus,
    ProductionResource,
)
from production.services.planning_conflicts import Conflict, ConflictCode, WARN
from production.services.schedule_geometry import (
    EPSILON_PCT,
    ScheduleWindow,
    fmt_pct,
    pack,
    split_overlaps,
)
from production.services.session_resources import machine_content_type_id
from production.services.working_calendar import WorkingCalendar

STATE_PLANNED = "planned"
STATE_RUNNING = "running"
STATE_DONE = "done"


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", (value or "").strip())


def ticket_color(ticket_id: int | None) -> str:
    if not ticket_id:
        return "#8b939e"
    return TICKET_PALETTE[ticket_id % len(TICKET_PALETTE)]


def stage_abbreviation(stage: str) -> str:
    cleaned = nfc(stage)
    words = [w for w in cleaned.replace("-", " ").split() if w]
    if len(words) >= 2:
        return "".join(w[0] for w in words[:3]).upper()
    return cleaned[:3].upper()


def text_color_for(background: str) -> str:
    """Black or white, whichever reads on *background*."""
    try:
        r, g, b = (int(background[i : i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return "#0e1013"
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#0e1013" if luminance > 0.6 else "#ffffff"


@dataclass
class UiBlock:
    session_id: int
    ticket_id: int | None
    ticket_label: str
    stage: str
    stage_abbr: str
    start: datetime
    end: datetime
    minutes: int
    sequence: int
    resource_ids: list[int]
    resource_labels: list[str]
    machine_labels: list[str]
    employee_labels: list[str]
    state: str
    is_late: bool
    is_locked: bool
    color: str
    left_pct: float
    width_pct: float
    conflicts: list[Conflict] = field(default_factory=list)
    is_displaced: bool = False
    # This block's lane within its row, as a slice of the row's height. Set by
    # split_overlaps() and fixed for the block's whole duration.
    top_pct: float = 0.0
    height_pct: float = 100.0

    @property
    def left(self) -> str:
        return fmt_pct(self.left_pct)

    @property
    def width(self) -> str:
        return fmt_pct(self.width_pct)

    @property
    def top(self) -> str:
        return fmt_pct(self.top_pct)

    @property
    def height(self) -> str:
        return fmt_pct(self.height_pct)

    @property
    def text_color(self) -> str:
        return text_color_for(self.color)

    @property
    def show_label(self) -> bool:
        """A block sharing its lane is too short on room for readable text."""
        return not self.is_combined

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    @property
    def is_combined(self) -> bool:
        """Whether this block shares its row with another stage."""
        return self.height_pct < 100.0 - EPSILON_PCT

    @property
    def border_status(self) -> str:
        """One unambiguous informational border per standalone stage."""
        # A combined block draws its status in the title only; a border per
        # underlying stage would read as a broken edge.
        if self.is_combined:
            return ""
        if self.is_done:
            return "done"
        if self.is_running:
            return "running"
        if self.is_late:
            return "overdue"
        if self.has_conflicts:
            return "warning"
        return ""

    @property
    def is_done(self) -> bool:
        return self.state == STATE_DONE

    @property
    def is_running(self) -> bool:
        return self.state == STATE_RUNNING

    @property
    def status_message(self) -> str:
        """The status line shown in a stage's hover text. Precedence mirrors border_status."""
        if self.is_done:
            return str(_("Completed"))
        if self.is_running:
            return str(_("In progress"))
        if self.is_late:
            return str(_("Overdue"))
        return ""

    @property
    def title(self) -> str:
        parts = [
            f"{self.ticket_label} · {self.stage}",
            f"{timezone.localtime(self.start):%d/%m %H:%M}"
            f" → {timezone.localtime(self.end):%H:%M}"
            f" ({self.minutes} min)",
        ]
        if self.resource_labels:
            parts.append(", ".join(self.resource_labels))
        if self.status_message:
            parts.append(self.status_message)
        # str(): Conflict.message is a gettext_lazy() proxy, not a real str.
        parts += [str(c.message) for c in self.conflicts]
        return "\n".join(parts)


@dataclass
class UiRow:
    key: str
    label: str
    sublabel: str
    kind: str  # "machine" | "employee" | "ticket"
    resource_id: int | None = None
    ticket_id: int | None = None
    blocks: list[UiBlock] = field(default_factory=list)


@dataclass
class UiMachineGroup:
    """One machine within a production stage on the daily board."""

    machine: UiRow
    operator_rows: list[UiRow]
    unassigned_row: UiRow | None = None

    @property
    def blocks(self) -> list[UiBlock]:
        return self.machine.blocks


@dataclass
class UiStageGroup:
    """One production stage, with its available machines and operators."""

    stage: str
    machine_groups: list[UiMachineGroup]
    operator_rows: list[UiRow] = field(default_factory=list)
    unassigned_machine_row: UiRow | None = None

    @property
    def has_single_machine(self) -> bool:
        return (
            len(self.machine_groups) == 1
            and self.unassigned_machine_row is None
        )

    @cached_property
    def blocks(self) -> list[UiBlock]:
        blocks: dict[int, UiBlock] = {}
        rows = list(self.operator_rows) or [
            machine.machine for machine in self.machine_groups
        ]
        if self.unassigned_machine_row is not None:
            rows.append(self.unassigned_machine_row)
        for row in rows:
            for block in row.blocks:
                blocks[block.session_id] = block

        # Overlaps here differ from the source rows, so this needs its own split.
        gathered = [_clone(block) for block in blocks.values()]
        gathered.sort(key=lambda block: block.start)
        split_overlaps(gathered)
        return gathered


@dataclass
class ScheduleData:
    window: ScheduleWindow
    machine_rows: list[UiRow]
    employee_rows: list[UiRow]
    ticket_rows: list[UiRow]
    stage_groups: list[UiStageGroup]
    now_pct: float | None
    has_schedule: bool

    @property
    def now(self) -> str:
        return fmt_pct(self.now_pct or 0)

    @property
    def is_empty(self) -> bool:
        return not (self.machine_rows or self.employee_rows)


class ProductionScheduleContext:
    """One expensive pass, shared by every calendar view."""

    def __init__(self, *, company, start: datetime, end: datetime, resource_ids=None):
        self.company = company
        self.start = start
        self.end = end
        self.resource_ids = set(resource_ids) if resource_ids else None
        self.calendar = WorkingCalendar.for_company(company)

    # --- public ----------------------------------------------------------

    def build(self) -> ScheduleData:
        window = ScheduleWindow.build(self.calendar, self.start, self.end)

        # Load the whole resource set; the row filter is applied later in Python.
        resources = self._load_resources()
        self._labels = {r.pk: self._resource_label(r) for r in resources}
        machine_ct = machine_content_type_id()
        self._kinds = {
            resource.pk: (
                "machine" if resource.content_type_id == machine_ct else "employee"
            )
            for resource in resources
        }
        self._resource_stages = {
            resource.pk: {
                nfc(stage)
                for stage in (getattr(resource.target, "production_stages", None) or [])
            }
            for resource in resources
            if resource.target is not None
        }
        self._production_employee_ids = {
            resource.pk
            for resource in resources
            if isinstance(resource.target, Employee)
            and resource.target.can_work_on_production
        }

        sessions = self._load_sessions()
        stage_states = self._stage_states(sessions)
        warnings_by_session = self._warning_conflicts(sessions, resources)

        blocks_by_session = {}
        for session in sessions:
            block = self._block(
                session,
                window,
                stage_states,
                warnings_by_session.get(session.pk, []),
            )
            if block is not None:
                blocks_by_session[session.pk] = block

        if self.resource_ids:
            resources = [
                resource for resource in resources if resource.pk in self.resource_ids
            ]

        machine_rows, employee_rows = self._resource_rows(resources, blocks_by_session)
        data = ScheduleData(
            window=window,
            machine_rows=machine_rows,
            employee_rows=employee_rows,
            ticket_rows=self._ticket_rows(sessions, blocks_by_session),
            stage_groups=self._stage_groups(
                machine_rows, employee_rows, blocks_by_session
            ),
            now_pct=window.now_pct(),
            has_schedule=self.calendar.has_slots,
        )

        # Last: the nested view derives its rows from the flat ones above.
        for row in _rows_to_lay_out(data):
            _lay_out(row)
        return data

    # --- queries ---------------------------------------------------------

    def _load_resources(self) -> list[ProductionResource]:
        return list(
            ProductionResource.objects.filter(
                company=self.company, is_active=True
            ).prefetch_related(
                GenericPrefetch(
                    "target", [Employee.objects.all(), Machine.objects.all()]
                )
            )
        )

    def _load_sessions(self) -> list[PlannedProductionSession]:
        return list(
            PlannedProductionSession.objects.filter(
                company=self.company,
                planned_start__lt=self.end,
                planned_end__gt=self.start,
            )
            .select_related("production_ticket__project_file__client")
            # Resource names come from the label map above, not from here.
            .prefetch_related(
                "resources",
                GenericPrefetch(
                    "production_ticket__source_object",
                    [
                        Configuration.objects.select_related(
                            "product", "quote__project_file__client"
                        )
                    ],
                ),
            )
            .order_by("planned_start", "sequence", "id")
        )

    def _stage_states(self, sessions) -> dict[tuple[int, str], str]:
        """Derive done/running per (ticket, stage) from ProductionItem.planning."""
        ticket_ids = {
            session.production_ticket_id
            for session in sessions
            if session.production_ticket_id
        }
        if not ticket_ids:
            return {}

        states: dict[tuple[int, str], list[str]] = {}
        items = ProductionItem.objects.filter(ticket_id__in=ticket_ids).values_list(
            "ticket_id", "planning"
        )
        for ticket_id, planning in items:
            for stage, info in (planning or {}).items():
                key = (ticket_id, nfc(stage))
                status = (info or {}).get("status") or ProductionItemStatus.PENDING
                states.setdefault(key, []).append(status)

        resolved = {}
        for key, statuses in states.items():
            if all(s == ProductionItemStatus.DONE for s in statuses):
                resolved[key] = STATE_DONE
            elif any(s == ProductionItemStatus.IN_PROGRESS for s in statuses):
                resolved[key] = STATE_RUNNING
            else:
                resolved[key] = STATE_PLANNED
        return resolved

    def _warning_conflicts(self, sessions, resources) -> dict[int, list[Conflict]]:
        """Warnings still relevant after confirmation, computed from data already loaded."""
        warnings = defaultdict(list)
        seen = defaultdict(set)

        def add(session, code, message):
            if code not in seen[session.pk]:
                warnings[session.pk].append(
                    Conflict(code=code, severity=WARN, message=message, session_id=session.pk)
                )
                seen[session.pk].add(code)

        resources_by_id = {resource.pk: resource for resource in resources}
        sessions_by_resource = defaultdict(list)
        for session in sessions:
            resource_ids = [assignment.resource_id for assignment in session.resources.all()]
            if (
                not self.calendar.is_working(session.planned_start)
                or not self.calendar.is_working(
                    session.planned_end - timedelta(minutes=1)
                )
            ):
                add(
                    session,
                    ConflictCode.OUTSIDE_WORKING_HOURS,
                    _("Starts or ends outside working hours."),
                )

            for resource_id in resource_ids:
                resource = resources_by_id.get(resource_id)
                target = resource.target if resource else None
                allowed = {
                    nfc(stage)
                    for stage in (getattr(target, "production_stages", None) or [])
                }
                if allowed and nfc(session.production_stage) not in allowed:
                    add(
                        session,
                        ConflictCode.RESOURCE_NOT_SKILLED,
                        _("An assigned resource is not configured for this stage."),
                    )
                sessions_by_resource[resource_id].append(session)

        for resource_id, assigned_sessions in sessions_by_resource.items():
            resource = resources_by_id.get(resource_id)
            if resource is None:
                continue
            capacity = resource.parallel_capacity or 1
            active = []
            for session in sorted(
                assigned_sessions, key=lambda item: (item.planned_start, item.pk)
            ):
                active = [
                    item for item in active if item.planned_end > session.planned_start
                ]
                if len(active) + 1 > capacity:
                    message = _("Assigned resource is over its parallel capacity.")
                    add(session, ConflictCode.RESOURCE_OVERLAP, message)
                    for overlapping in active:
                        add(overlapping, ConflictCode.RESOURCE_OVERLAP, message)
                active.append(session)

            if resource.daily_capacity_minutes:
                day_start = self.start
                day_end = self.end
                used = sum(
                    self.calendar.working_minutes_between(
                        max(session.planned_start, day_start),
                        min(session.planned_end, day_end),
                    )
                    for session in assigned_sessions
                )
                if used > resource.daily_capacity_minutes:
                    for session in assigned_sessions:
                        add(
                            session,
                            ConflictCode.DAILY_CAPACITY_EXCEEDED,
                            _("Assigned resource exceeds its daily capacity."),
                        )

        # Load sibling stages in one query to check sequence order.
        ticket_ids = {session.production_ticket_id for session in sessions}
        siblings_by_ticket = defaultdict(list)
        for sibling in PlannedProductionSession.objects.filter(
            production_ticket_id__in=ticket_ids
        ).only("id", "production_ticket_id", "sequence", "planned_start", "planned_end"):
            siblings_by_ticket[sibling.production_ticket_id].append(sibling)

        for session in sessions:
            siblings = siblings_by_ticket[session.production_ticket_id]
            previous = [item for item in siblings if item.sequence < session.sequence]
            following = [item for item in siblings if item.sequence > session.sequence]
            if previous and session.planned_start < max(
                item.planned_end for item in previous
            ):
                add(
                    session,
                    ConflictCode.STAGE_ORDER_VIOLATION,
                    _("Starts before an earlier production stage finishes."),
                )
            if following and session.planned_end > min(
                item.planned_start for item in following
            ):
                add(
                    session,
                    ConflictCode.STAGE_ORDER_VIOLATION,
                    _("Ends after a later production stage starts."),
                )

        return warnings

    # --- shaping ---------------------------------------------------------

    def _ticket_label(self, ticket) -> str:
        return _("Unassigned") if ticket is None else ticket.display_label

    def _resource_label(self, resource) -> str:
        return str(resource.target) if resource.target else f"#{resource.pk}"

    def _block(self, session, window, stage_states, warnings=()) -> UiBlock | None:
        if window.is_empty:
            return None

        resource_ids = [a.resource_id for a in session.resources.all()]
        resource_labels = [
            self._labels.get(resource_id, f"#{resource_id}")
            for resource_id in resource_ids
        ]
        machine_labels = [
            self._labels.get(resource_id, f"#{resource_id}")
            for resource_id in resource_ids
            if self._kinds.get(resource_id) == "machine"
        ]
        employee_labels = [
            self._labels.get(resource_id, f"#{resource_id}")
            for resource_id in resource_ids
            if self._kinds.get(resource_id) == "employee"
        ]

        state = stage_states.get(
            (session.production_ticket_id, nfc(session.production_stage)),
            STATE_PLANNED,
        )
        is_late = state != STATE_DONE and session.planned_end < timezone.now()

        left, width = window.block(session.planned_start, session.planned_end)

        block = UiBlock(
            session_id=session.pk,
            ticket_id=session.production_ticket_id,
            ticket_label=self._ticket_label(session.production_ticket),
            stage=session.production_stage,
            stage_abbr=stage_abbreviation(session.production_stage),
            start=session.planned_start,
            end=session.planned_end,
            minutes=session.duration_minutes,
            sequence=session.sequence,
            resource_ids=resource_ids,
            resource_labels=resource_labels,
            machine_labels=machine_labels,
            employee_labels=employee_labels,
            state=state,
            is_late=is_late,
            is_locked=session.locked,
            color=ticket_color(session.production_ticket_id),
            left_pct=left,
            width_pct=width,
            conflicts=list(warnings),
        )

        return block

    def _resource_rows(self, resources, blocks_by_session):
        machine_ct = machine_content_type_id()

        machines: list[UiRow] = []
        employees: list[UiRow] = []

        rows_by_resource: dict[int, UiRow] = {}
        for resource in sorted(resources, key=lambda r: self._resource_label(r).lower()):
            is_machine = resource.content_type_id == machine_ct
            row = UiRow(
                key=f"resource-{resource.pk}",
                label=self._resource_label(resource),
                sublabel=_("Machine") if is_machine else _("Operator"),
                kind="machine" if is_machine else "employee",
                resource_id=resource.pk,
            )
            rows_by_resource[resource.pk] = row
            (machines if is_machine else employees).append(row)

        # Appended to every row it belongs to, so one session can render twice.
        for block in blocks_by_session.values():
            for resource_id in block.resource_ids:
                row = rows_by_resource.get(resource_id)
                if row is not None:
                    row.blocks.append(block)

        return machines, employees

    def _stage_groups(
        self, machine_rows, employee_rows, blocks_by_session
    ) -> list[UiStageGroup]:
        """Group company stages; pair machines and operators only when both support one."""
        employees = {row.resource_id: row for row in employee_rows}
        stage_names = list(
            dict.fromkeys(
                stage
                for stage in (nfc(value) for value in (self.company.production_stages or []))
                if stage
            )
        )
        groups = []
        for stage in stage_names:
            machine_groups = [
                self._stage_machine_group(stage, machine, employees)
                for machine in machine_rows
                if stage in self._resource_stages.get(machine.resource_id, set())
                or any(nfc(block.stage) == stage for block in machine.blocks)
            ]
            operator_rows = []
            unassigned_machine_row = None
            if not machine_groups:
                operator_rows = self._stage_operator_rows(
                    stage,
                    employees,
                    {
                        employee_id: [
                            block
                            for block in employee.blocks
                            if nfc(block.stage) == stage
                            and not any(
                                self._kinds.get(resource_id) == "machine"
                                for resource_id in block.resource_ids
                            )
                        ]
                        for employee_id, employee in employees.items()
                    },
                    key_prefix=f"stage-{stage}",
                )
            else:
                blocks_without_machine = [
                    block
                    for block in blocks_by_session.values()
                    if nfc(block.stage) == stage
                    and not any(
                        self._kinds.get(resource_id) == "machine"
                        for resource_id in block.resource_ids
                    )
                ]
                if blocks_without_machine:
                    unassigned_machine_row = UiRow(
                        key=f"stage-{stage}-unassigned-machine",
                        label=_("Unassigned machine"),
                        sublabel=_("Needs attention"),
                        kind="unassigned",
                        blocks=sorted(
                            blocks_without_machine, key=lambda block: block.start
                        ),
                    )
            groups.append(
                UiStageGroup(
                    stage=stage,
                    machine_groups=machine_groups,
                    operator_rows=operator_rows,
                    unassigned_machine_row=unassigned_machine_row,
                )
            )
        return groups

    def _stage_machine_group(self, stage, machine, employees) -> UiMachineGroup:
        machine_blocks = [
            block for block in machine.blocks if nfc(block.stage) == stage
        ]
        blocks_by_employee = {employee_id: [] for employee_id in employees}
        unassigned = []
        for block in machine_blocks:
            employee_ids = [
                resource_id
                for resource_id in block.resource_ids
                if resource_id in employees
            ]
            if not employee_ids:
                unassigned.append(block)
            for employee_id in employee_ids:
                blocks_by_employee[employee_id].append(block)

        return UiMachineGroup(
            machine=UiRow(
                key=f"stage-{stage}-machine-{machine.resource_id}",
                label=machine.label,
                sublabel=_("Machine"),
                kind="machine",
                resource_id=machine.resource_id,
                blocks=machine_blocks,
            ),
            operator_rows=self._stage_operator_rows(
                stage,
                employees,
                blocks_by_employee,
                key_prefix=f"stage-{stage}-machine-{machine.resource_id}",
            ),
            unassigned_row=(
                UiRow(
                    key=f"stage-{stage}-machine-{machine.resource_id}-unassigned",
                    label=_("Unassigned operator"),
                    sublabel=_("Needs attention"),
                    kind="unassigned",
                    blocks=sorted(unassigned, key=lambda block: block.start),
                )
                if unassigned
                else None
            ),
        )

    def _stage_operator_rows(self, stage, employees, blocks_by_employee, *, key_prefix):
        operator_blocks = {
            employee_id: []
            for employee_id in employees
            if employee_id in self._production_employee_ids
            and stage in self._resource_stages.get(employee_id, set())
        }
        # Don't hide a historical assignment just because capability config later changed.
        for employee_id, blocks in blocks_by_employee.items():
            if blocks:
                operator_blocks.setdefault(employee_id, []).extend(blocks)

        return [
            UiRow(
                key=f"{key_prefix}-employee-{employee_id}",
                label=employees[employee_id].label,
                sublabel=_("Operator"),
                kind="employee",
                resource_id=employee_id,
                blocks=sorted(blocks, key=lambda block: block.start),
            )
            for employee_id, blocks in sorted(
                operator_blocks.items(),
                key=lambda item: employees[item[0]].label.lower(),
            )
        ]

    def _ticket_rows(self, sessions, blocks_by_session) -> list[UiRow]:
        rows: dict[int, UiRow] = {}

        for session in sessions:
            ticket_id = session.production_ticket_id
            block = blocks_by_session.get(session.pk)
            if block is None or ticket_id is None:
                continue

            row = rows.get(ticket_id)
            if row is None:
                row = UiRow(
                    key=f"ticket-{ticket_id}",
                    label=f"OP-{ticket_id}",
                    sublabel=block.ticket_label,
                    kind="ticket",
                    ticket_id=ticket_id,
                )
                rows[ticket_id] = row

            # A ticket keeps its colour in every view, unlike other rows.
            row.blocks.append(_clone(block))

        for row in rows.values():
            row.blocks.sort(key=lambda b: (b.sequence, b.start))
            pack(row.blocks)

        # Rows only exist once they hold a block, so blocks[0] is always there.
        return sorted(rows.values(), key=lambda r: r.blocks[0].start)


def _clone(block: UiBlock, *, color: str | None = None) -> UiBlock:
    """An independent copy, optionally recoloured. Rows cannot share block objects."""
    clone = UiBlock(**{**block.__dict__, "color": color or block.color})
    clone.conflicts = list(block.conflicts)
    return clone


def _lay_out(row: UiRow) -> None:
    """Give *row* its own blocks, split wherever they run at the same time."""
    row.blocks = [_clone(block) for block in row.blocks]
    split_overlaps(row.blocks)


def _rows_to_lay_out(data: ScheduleData):
    """Every row that shows true clock time -- ticket rows use packed geometry instead."""
    yield from data.machine_rows
    yield from data.employee_rows
    for group in data.stage_groups:
        yield from group.operator_rows
        if group.unassigned_machine_row is not None:
            yield group.unassigned_machine_row
        for machine_group in group.machine_groups:
            yield machine_group.machine
            yield from machine_group.operator_rows
            if machine_group.unassigned_row is not None:
                yield machine_group.unassigned_row
