"""
Endpoints for the shared production terminal.

All views use SharedTerminalAPIView (requires can_use_shared_terminal on CompanyUser).
Mutations require the target employee's 4-digit work_pin in the request body.
"""

import json
import unicodedata as _uc

from django.contrib.auth.hashers import check_password
from django.db.models import F, JSONField, Value
from django.db.models.expressions import CombinedExpression
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from api._serializers import active_ticket_items, _ordered_stage_names
from api._utils import SharedTerminalAPIView
from company.models import Employee
from configurator.models import Configuration, ConfigurationStatus
from management.models import ProjectFileStatus
from production.exceptions import StageHasActiveWorkersError
from production.models import (
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionTicket,
    ProductionTicketStatus,
)
from production.services.stages_to_sessions import StagesToSessionsService


# ─── Employee list ────────────────────────────────────────────────────────────


class TerminalEmployeeListView(SharedTerminalAPIView):
    def get(self, request):
        employees = (
            Employee.objects.filter(
                company=self.company,
                can_work_on_production=True,
                is_active=True,
            )
            .order_by("last_name", "first_name")
        )
        return JsonResponse({
            "employees": [
                {
                    "id": e.id,
                    "first_name": e.first_name,
                    "last_name": e.last_name,
                    "full_name": e.full_name,
                }
                for e in employees
            ],
            # Company-level terminal config, piggybacked on the bootstrap call.
            "pause_reasons": self.company.pause_reasons or [],
        })


# ─── Task list for a given employee ──────────────────────────────────────────


def _employee_can_work_stage(employee: Employee, stage_name_nfc: str) -> bool:
    allowed = [_uc.normalize("NFC", s) for s in (employee.production_stages or [])]
    return not allowed or stage_name_nfc in allowed


def _build_stage_history_indexes(item_ids) -> tuple[dict[tuple, list[dict]], dict[tuple, list]]:
    """One sessions query -> (comments index, sessions index), keyed by (item_id, stage_nfc).

    Comments are ordered dicts with author and timestamp; sessions are model
    instances ordered by start_time (sessions without start_time are skipped).
    """
    comments_index: dict[tuple, list[dict]] = {}
    sessions_index: dict[tuple, list] = {}
    for sess in (
        ProductionItemSession.objects.filter(production_item_id__in=item_ids)
        .select_related("created_by")
        .order_by("production_item_id", "production_stage", "start_time")
    ):
        emp = sess.created_by
        if emp is None:
            continue
        key = (sess.production_item_id, _uc.normalize("NFC", sess.production_stage))
        if sess.start_time:
            sessions_index.setdefault(key, []).append(sess)
        for entry in sess.comment_entries or []:
            if not isinstance(entry, dict):
                continue
            text = (entry.get("text") or "").strip()
            if text:
                comments_index.setdefault(key, []).append({
                    "employee_name": emp.full_name,
                    "comment": text,
                    "at": entry.get("at"),
                })
    return comments_index, sessions_index


def _build_session_comments_index(item_ids) -> dict[tuple, list[dict]]:
    """(item_id, stage_nfc) -> ordered comment dicts with author and timestamp."""
    comments_index, _ = _build_stage_history_indexes(item_ids)
    return comments_index


def _stage_events(sessions, stage_status: str) -> list[dict]:
    """Chronological start/resume/pause/finish events derived from sessions.

    The last close of a done stage is the finish; any other close is a pause
    (cancelled sessions are auto-closed leftovers and labelled as such).
    """
    last_end = max((s.end_time for s in sessions if s.end_time), default=None)
    events: list[dict] = []
    for i, sess in enumerate(sessions):
        emp_name = sess.created_by.full_name if sess.created_by else "—"
        events.append({
            "type": "started" if i == 0 else "resumed",
            "employee_name": emp_name,
            "at": sess.start_time.isoformat(),
        })
        if sess.end_time:
            if sess.status == ProductionItemSessionStatus.CANCELLED:
                end_type = "cancelled"
            elif stage_status == "done" and sess.end_time == last_end:
                end_type = "finished"
            else:
                end_type = "paused"
            events.append({
                "type": end_type,
                "employee_name": emp_name,
                "at": sess.end_time.isoformat(),
            })
    events.sort(key=lambda e: e["at"])
    return events


def _build_task_results(employee: Employee, company, search: str = "") -> list[dict]:
    """
    Returns the same shape as MyTasksView but parameterised on any employee.
    Adds `is_assigned` to each next_stage so the UI can split Assigned / Available.
    """
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(Configuration)

    active_config_ids = list(
        Configuration.objects.filter(
            company=company,
            status__in=(ConfigurationStatus.CONVERTED, ConfigurationStatus.SHOP),
        ).values_list("id", flat=True)
    )

    tickets = list(
        ProductionTicket.objects.filter(
            company=company,
            content_type=ct,
            object_id__in=active_config_ids,
        )
        .select_related("project_file__client")
        .prefetch_related("items__assigned_employees", "items__component")
    )

    # IDs of items explicitly assigned to this employee by a manager.
    assigned_item_ids: set[int] = set(
        employee.assigned_production_items.filter(
            ticket__company=company,
        ).values_list("id", flat=True)
    )

    # Open sessions for all items in the company — one DB query.
    open_sessions = (
        ProductionItemSession.objects.filter(
            production_item__ticket__company=company,
            end_time__isnull=True,
            status=ProductionItemSessionStatus.IN_PROGRESS,
        )
        .select_related("created_by")
    )
    workers_index: dict[tuple, list[dict]] = {}
    for sess in open_sessions:
        emp = sess.created_by
        if emp is None:
            continue
        key = (sess.production_item_id, _uc.normalize("NFC", sess.production_stage))
        workers_index.setdefault(key, []).append({"id": emp.id, "full_name": emp.full_name})

    # All session comments for the employee's visible items, oldest first — one DB query.
    # Includes comments from all employees (not just this one) so coworker notes are visible.
    employee_item_ids: set[int] = set()
    for ticket in tickets:
        for item in active_ticket_items(ticket):
            employee_item_ids.add(item.id)

    # Session comments: each entry carries its own timestamp ("at" is null for legacy notes).
    session_comments_index = _build_session_comments_index(employee_item_ids)

    # Items where this employee has ANY session (open or closed) — for Reanudar vs Unirse logic.
    my_session_keys: set[tuple] = {
        (item_id, _uc.normalize("NFC", stage or ""))
        for item_id, stage in ProductionItemSession.objects.filter(
            created_by=employee,
            production_item_id__in=employee_item_ids,
        ).values_list("production_item_id", "production_stage")
    }

    results: list[dict] = []

    for ticket in tickets:
        if ticket.status == ProductionTicketStatus.CANCELLED:
            continue

        config = ticket.source_object
        if config is None:
            continue

        items = active_ticket_items(ticket)

        project_file = ticket.project_file
        if project_file is None:
            try:
                project_file = config.quote.project_file
            except Exception:
                project_file = None

        if project_file is not None and project_file.status == ProjectFileStatus.CANCELLED:
            continue

        if search and project_file:
            client_name = project_file.client.name if project_file.client else ""
            if (
                search.lower() not in (project_file.name or "").lower()
                and search.lower() not in client_name.lower()
            ):
                continue

        # Build (item, stage) pairs this employee can work.
        pairs = []
        for ii, item in enumerate(items):
            data = item.data or {}
            component_label = data.get("component") or (
                str(item.component) if getattr(item, "component_id", None) else "—"
            )
            for jj, stage_name in enumerate(_ordered_stage_names(item)):
                if not _employee_can_work_stage(employee, stage_name):
                    continue
                raw = {}
                for k, v in (item.planning or {}).items():
                    if _uc.normalize("NFC", k) == stage_name:
                        raw = v if isinstance(v, dict) else {}
                        break
                status = (raw.get("status") or "pending") if isinstance(raw, dict) else "pending"
                is_assigned = item.id in assigned_item_ids
                # Employees only see assigned tasks; in-progress/done always show.
                if status == "pending" and not is_assigned:
                    continue
                pairs.append({
                    "sort": (ii, jj),
                    "production_item_id": item.id,
                    "stage_name": stage_name,
                    "status": status,
                    "component_label": component_label,
                    "is_assigned": is_assigned,
                    "pause_reason": raw.get("pause_reason") if isinstance(raw, dict) else None,
                })

        pairs.sort(key=lambda p: p["sort"])
        if not pairs:
            continue

        quote = getattr(config, "quote", None)
        quote_name = (
            (quote.project_name or "").strip()
            if quote and (quote.project_name or "").strip()
            else (getattr(config, "name", None) or "")
        )

        expediente_data = None
        if project_file:
            start_date = project_file.start_date
            expediente_data = {
                "id": project_file.id,
                "reference": getattr(project_file, "reference", None) or "",
                "name": project_file.name or "",
                "client_name": project_file.client.name if project_file.client else None,
                "start_date": (
                    start_date.strftime("%Y-%m-%d")
                    if hasattr(start_date, "strftime")
                    else start_date
                ),
            }

        base = {
            "config_id": config.id,
            "config_name": getattr(config, "name", None) or "",
            "config_notes": getattr(config, "notes", None) or "",
            "quote_name": quote_name,
            "template_name": config.product.name if getattr(config, "product", None) else None,
            "ticket_status": ticket.status,
            "expediente": expediente_data,
            "my_stages_total": len(pairs),
            "my_stages_done": sum(1 for p in pairs if p["status"] == "done"),
        }

        for stage_p in pairs:
            stage_name_n = _uc.normalize("NFC", stage_p["stage_name"])
            workers = workers_index.get((stage_p["production_item_id"], stage_name_n), [])
            has_my_open_session = any(w["id"] == employee.id for w in workers)
            has_my_prev_session = (
                stage_p["production_item_id"],
                stage_name_n,
            ) in my_session_keys
            stage_comments = session_comments_index.get(
                (stage_p["production_item_id"], stage_name_n), []
            )
            results.append({
                **base,
                "next_stage": {
                    "name": stage_p["stage_name"],
                    "status": stage_p["status"],
                    "production_item_id": stage_p["production_item_id"],
                    "component_label": stage_p["component_label"],
                    "active_workers": workers,
                    "has_my_open_session": has_my_open_session,
                    "has_my_prev_session": has_my_prev_session,
                    # True only when others are working AND I've never touched this stage
                    "started_by_others": (
                        stage_p["status"] == "in_progress"
                        and bool(workers)
                        and not has_my_open_session
                        and not has_my_prev_session
                    ),
                    "is_assigned": stage_p["is_assigned"],
                    "session_comments": stage_comments,
                    "pause_reason": stage_p["pause_reason"],
                },
            })

    return results


class TerminalEmployeeTasksView(SharedTerminalAPIView):
    def get(self, request, employee_id):
        employee = get_object_or_404(
            Employee,
            id=employee_id,
            company=self.company,
            can_work_on_production=True,
            is_active=True,
        )
        search = request.GET.get("search", "").strip()
        results = _build_task_results(employee, self.company, search=search)
        return JsonResponse({"results": results, "count": len(results)})


# ─── Stage mutations (start / leave / finish) ─────────────────────────────────


def _verify_pin(employee: Employee, pin: str) -> bool:
    return bool(employee.work_pin) and check_password(pin, employee.work_pin)


def _get_employee_and_item(employee_id, item_id, company):
    """Returns (employee, item) or raises Http404."""
    from production.models import ProductionItem

    employee = get_object_or_404(
        Employee,
        id=employee_id,
        company=company,
        can_work_on_production=True,
        is_active=True,
    )
    item = get_object_or_404(ProductionItem, id=item_id, company=company)
    return employee, item


class _TerminalStageActionView(SharedTerminalAPIView):
    """
    Base class for PIN-authenticated stage mutations.
    Subclasses implement `_run(stage_name, item, employee, body) -> int`.
    """

    def post(self, request, employee_id, item_id, stage_name):
        body = json.loads(request.body or b"{}")
        pin = str(body.get("pin", "")).strip()

        employee, item = _get_employee_and_item(employee_id, item_id, self.company)

        if not _verify_pin(employee, pin):
            return JsonResponse(
                {"error": "PIN incorrecto.", "code": "invalid_pin"},
                status=403,
            )

        stage_name_n = _uc.normalize("NFC", stage_name)

        try:
            count = self._run(
                stage_name=stage_name_n, item=item, employee=employee, body=body
            )
        except StageHasActiveWorkersError as e:
            return JsonResponse(
                {
                    "error": e.message,
                    "code": StageHasActiveWorkersError.code,
                    "active_workers": e.active_workers,
                },
                status=409,
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({"success": True, "count": count})

    def _run(self, *, stage_name: str, item, employee: Employee, body: dict) -> int:
        raise NotImplementedError


class TerminalStageNoteView(SharedTerminalAPIView):
    def post(self, request, employee_id, item_id, stage_name):
        body = json.loads(request.body or b"{}")
        comments = str(body.get("comments", "")).strip()

        if not comments:
            return JsonResponse({"error": "El comentario no puede estar vacío."}, status=400)

        employee = get_object_or_404(
            Employee,
            id=employee_id,
            company=self.company,
            can_work_on_production=True,
            is_active=True,
        )

        stage_name_n = _uc.normalize("NFC", stage_name)

        # Find the most recent session for this employee on this stage,
        # open or closed (needed when the stage is paused).
        session = (
            ProductionItemSession.objects.filter(
                production_item_id=item_id,
                production_item__company=self.company,
                production_stage=stage_name_n,
                created_by=employee,
            )
            .order_by("-start_time")
            .first()
        )

        if not session:
            return JsonResponse({"error": "No tienes ninguna sesión en esta etapa."}, status=404)

        entry = {"text": comments, "at": timezone.now().isoformat()}
        ProductionItemSession.objects.filter(pk=session.pk).update(
            comment_entries=CombinedExpression(
                F("comment_entries"),
                "||",
                Value([entry], output_field=JSONField()),
            )
        )

        return JsonResponse({"success": True})


class TerminalStageStartView(_TerminalStageActionView):
    def _run(self, *, stage_name, item, employee, body):
        # skip_worker_check=True: terminal allows starting a new task
        # even if the employee forgot to close a previous session.
        return StagesToSessionsService.start_activity(
            stage_name=stage_name,
            items=[item],
            employee=employee,
            skip_worker_check=True,
        )


class TerminalStageLeaveView(_TerminalStageActionView):
    def _run(self, *, stage_name, item, employee, body):
        return StagesToSessionsService.leave_activity(
            stage_name=stage_name,
            items=[item],
            employee=employee,
            pause_reason=body.get("pause_reason"),
        )


class TerminalStageFinishView(_TerminalStageActionView):
    def _run(self, *, stage_name, item, employee, body):
        return StagesToSessionsService.finish_activity(
            stage_name=stage_name,
            items=[item],
            employee=employee,
        )


# ─── Employee PIN verify ─────────────────────────────────────────────────────


class EmployeePinVerifyView(SharedTerminalAPIView):
    def post(self, request, employee_id):
        body = json.loads(request.body or b"{}")
        pin = str(body.get("pin", "")).strip()

        employee = get_object_or_404(
            Employee,
            id=employee_id,
            company=self.company,
            can_work_on_production=True,
            is_active=True,
        )

        if not _verify_pin(employee, pin):
            return JsonResponse(
                {"error": "PIN incorrecto.", "code": "invalid_pin"},
                status=403,
            )

        return JsonResponse({"success": True})


# ─── Global production overview ───────────────────────────────────────────────


def _build_all_task_results(company) -> dict[str, list[dict]]:
    """
    Returns all production stages for the company grouped by status:
    active (in_progress + workers), paused (in_progress + no workers),
    pending, done.
    """
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(Configuration)

    active_config_ids = list(
        Configuration.objects.filter(
            company=company,
            status__in=(
                ConfigurationStatus.CONVERTED,
                ConfigurationStatus.SHOP,
                ConfigurationStatus.PRODUCTION_DONE,
            ),
        ).values_list("id", flat=True)
    )

    tickets = list(
        ProductionTicket.objects.filter(
            company=company,
            content_type=ct,
            object_id__in=active_config_ids,
        )
        .select_related("project_file__client")
        .prefetch_related("items__component")
    )

    open_sessions = (
        ProductionItemSession.objects.filter(
            production_item__ticket__company=company,
            end_time__isnull=True,
            status=ProductionItemSessionStatus.IN_PROGRESS,
        )
        .select_related("created_by")
    )
    workers_index: dict[tuple, list[dict]] = {}
    for sess in open_sessions:
        emp = sess.created_by
        if emp is None:
            continue
        key = (sess.production_item_id, _uc.normalize("NFC", sess.production_stage))
        workers_index.setdefault(key, []).append({"id": emp.id, "full_name": emp.full_name})

    # Session comments + sessions — scoped to active tickets only so the query stays bounded.
    active_item_ids = [
        item.id
        for ticket in tickets
        for item in active_ticket_items(ticket)
    ]
    comments_index, sessions_index = _build_stage_history_indexes(active_item_ids)

    active: list[dict] = []
    paused: list[dict] = []
    assigned: list[dict] = []
    pending: list[dict] = []
    done: list[dict] = []

    for ticket in tickets:
        if ticket.status == ProductionTicketStatus.CANCELLED:
            continue

        config = ticket.source_object
        if config is None:
            continue

        items = active_ticket_items(ticket)

        project_file = ticket.project_file
        if project_file is None:
            try:
                project_file = config.quote.project_file
            except Exception:
                project_file = None

        if project_file is not None and project_file.status == ProjectFileStatus.CANCELLED:
            continue

        quote = getattr(config, "quote", None)
        quote_name = (
            (quote.project_name or "").strip()
            if quote and (quote.project_name or "").strip()
            else (getattr(config, "name", None) or "")
        )

        expediente_data = None
        if project_file:
            start_date = project_file.start_date
            expediente_data = {
                "id": project_file.id,
                "reference": getattr(project_file, "reference", None) or "",
                "name": project_file.name or "",
                "client_name": project_file.client.name if project_file.client else None,
                "start_date": (
                    start_date.strftime("%Y-%m-%d")
                    if hasattr(start_date, "strftime")
                    else start_date
                ),
            }

        base = {
            "config_id": config.id,
            "config_name": getattr(config, "name", None) or "",
            "config_notes": getattr(config, "notes", None) or "",
            "quote_name": quote_name,
            "template_name": config.product.name if getattr(config, "product", None) else None,
            "expediente": expediente_data,
        }

        for item in items:
            data = item.data or {}
            component_label = data.get("component") or (
                str(item.component) if getattr(item, "component_id", None) else "—"
            )
            for stage_name in _ordered_stage_names(item):
                raw = {}
                for k, v in (item.planning or {}).items():
                    if _uc.normalize("NFC", k) == stage_name:
                        raw = v if isinstance(v, dict) else {}
                        break
                status = (raw.get("status") or "pending") if isinstance(raw, dict) else "pending"

                stage_name_n = _uc.normalize("NFC", stage_name)
                workers = workers_index.get((item.id, stage_name_n), [])
                assigned_emps = [
                    {"id": e.id, "full_name": e.full_name}
                    for e in item.assigned_employees.all()
                    if _employee_can_work_stage(e, stage_name_n)
                ]

                entry = {
                    **base,
                    "stage": {
                        "name": stage_name,
                        "production_item_id": item.id,
                        "component_label": component_label,
                        "status": status,
                        "active_workers": workers,
                        "assigned_employees": assigned_emps,
                        "session_comments": comments_index.get((item.id, stage_name_n), []),
                        "session_events": _stage_events(
                            sessions_index.get((item.id, stage_name_n), []), status
                        ),
                        "pause_reason": raw.get("pause_reason") if isinstance(raw, dict) else None,
                    },
                }

                if status == "done":
                    done.append(entry)
                elif status == "in_progress":
                    if workers:
                        active.append(entry)
                    else:
                        paused.append(entry)
                elif assigned_emps:
                    assigned.append(entry)
                else:
                    pending.append(entry)

    return {
        "active": active,
        "paused": paused,
        "assigned": assigned,
        "pending": pending,
        "done": done,
    }


class TerminalAllTasksView(SharedTerminalAPIView):
    def get(self, request):
        result = _build_all_task_results(self.company)
        return JsonResponse(result)


def _session_worked_seconds(session: ProductionItemSession, now) -> int:
    """Worked time for one session; paused time (no end_time on closed rows) never counts."""
    if session.status == ProductionItemSessionStatus.CANCELLED:
        return 0
    if not session.start_time:
        return 0
    if session.end_time is None:
        if session.status != ProductionItemSessionStatus.IN_PROGRESS:
            return 0
        end = now
    else:
        end = session.end_time
    return max(0, int((end - session.start_time).total_seconds()))


def _build_production_hours_results(company) -> list[dict]:
    """Aggregate real worked time per presupuesto (configuration)."""
    from collections import defaultdict
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(Configuration)
    now = timezone.now()

    active_config_ids = list(
        Configuration.objects.filter(
            company=company,
            status__in=(
                ConfigurationStatus.CONVERTED,
                ConfigurationStatus.SHOP,
                ConfigurationStatus.PRODUCTION_DONE,
            ),
        ).values_list("id", flat=True)
    )

    tickets = list(
        ProductionTicket.objects.filter(
            company=company,
            content_type=ct,
            object_id__in=active_config_ids,
        )
        .select_related("project_file__client")
        .prefetch_related("items")
    )

    config_ids = [t.object_id for t in tickets if t.object_id]
    configs = {
        c.id: c
        for c in Configuration.objects.filter(pk__in=config_ids).select_related("quote")
    }

    ticket_by_config: dict[int, ProductionTicket] = {}
    item_ids: list[int] = []
    for ticket in tickets:
        if ticket.status == ProductionTicketStatus.CANCELLED:
            continue
        if ticket.object_id:
            ticket_by_config[ticket.object_id] = ticket
        for item in active_ticket_items(ticket):
            item_ids.append(item.id)

    sessions = (
        ProductionItemSession.objects.filter(
            production_item_id__in=item_ids,
        )
        .select_related("created_by", "production_item__ticket")
        .order_by("start_time")
    )

    seconds_by_config: dict[int, int] = defaultdict(int)
    open_by_config: dict[int, int] = defaultdict(int)
    employees_by_config: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for sess in sessions:
        item = sess.production_item
        ticket = item.ticket
        config_id = ticket.object_id
        if config_id not in ticket_by_config:
            continue

        worked = _session_worked_seconds(sess, now)
        if worked:
            seconds_by_config[config_id] += worked
            emp = sess.created_by
            if emp is not None:
                employees_by_config[config_id][emp.full_name] += worked

        if (
            sess.end_time is None
            and sess.status == ProductionItemSessionStatus.IN_PROGRESS
        ):
            open_by_config[config_id] += 1

    results: list[dict] = []
    for config_id, total_seconds in seconds_by_config.items():
        config = configs.get(config_id)
        ticket = ticket_by_config.get(config_id)
        if config is None or ticket is None:
            continue

        project_file = ticket.project_file
        if project_file is None:
            try:
                project_file = config.quote.project_file
            except Exception:
                project_file = None

        if project_file is not None and project_file.status == ProjectFileStatus.CANCELLED:
            continue

        quote = getattr(config, "quote", None)
        quote_name = (
            (quote.project_name or "").strip()
            if quote and (quote.project_name or "").strip()
            else (getattr(config, "name", None) or "")
        )

        expediente_data = None
        if project_file:
            start_date = project_file.start_date
            expediente_data = {
                "id": project_file.id,
                "reference": getattr(project_file, "reference", None) or "",
                "name": project_file.name or "",
                "client_name": project_file.client.name if project_file.client else None,
                "start_date": (
                    start_date.strftime("%Y-%m-%d")
                    if hasattr(start_date, "strftime")
                    else start_date
                ),
            }

        emp_seconds = employees_by_config.get(config_id, {})
        employees = [
            {"full_name": name, "seconds": secs}
            for name, secs in sorted(emp_seconds.items(), key=lambda x: (-x[1], x[0]))
        ]

        results.append({
            "config_id": config_id,
            "config_name": getattr(config, "name", None) or "",
            "config_notes": getattr(config, "notes", None) or "",
            "quote_name": quote_name,
            "ticket_status": ticket.status,
            "expediente": expediente_data,
            "total_seconds": total_seconds,
            "open_sessions": open_by_config.get(config_id, 0),
            "employees": employees,
        })

    results.sort(key=lambda r: (-r["total_seconds"], r["quote_name"]))
    return results


class TerminalProductionHoursView(SharedTerminalAPIView):
    def get(self, request):
        from django.core.paginator import Paginator

        all_results = _build_production_hours_results(self.company)
        total_seconds = sum(r["total_seconds"] for r in all_results)

        try:
            page_num = max(1, int(request.GET.get("page", 1)))
        except (TypeError, ValueError):
            page_num = 1
        try:
            raw_size = int(request.GET.get("page_size", 20))
        except (TypeError, ValueError):
            raw_size = 20
        page_size = max(1, min(50, raw_size))

        paginator = Paginator(all_results, page_size)
        page = paginator.get_page(page_num)

        return JsonResponse({
            "results": list(page.object_list),
            "count": paginator.count,
            "total_seconds": total_seconds,
            "page": page_num,
            "page_size": page_size,
            "has_next": page.has_next(),
        })


# ─── Supervisor: list ─────────────────────────────────────────────────────────

class SupervisorListView(SharedTerminalAPIView):
    def get(self, request):
        supervisors = Employee.objects.filter(
            company=self.company,
            is_production_supervisor=True,
            is_active=True,
        ).order_by("last_name", "first_name")
        return JsonResponse({
            "supervisors": [
                {"id": e.id, "full_name": e.full_name}
                for e in supervisors
            ]
        })


# ─── Supervisor: verify PIN ───────────────────────────────────────────────────

class SupervisorVerifyView(SharedTerminalAPIView):
    def post(self, request):
        body = json.loads(request.body or b"{}")
        supervisor_id = body.get("supervisor_id")
        pin = str(body.get("pin", "")).strip()

        try:
            supervisor = Employee.objects.get(
                id=supervisor_id,
                company=self.company,
                is_production_supervisor=True,
                is_active=True,
            )
        except Employee.DoesNotExist:
            return JsonResponse({"error": "Supervisor no encontrado."}, status=404)

        if not _verify_pin(supervisor, pin):
            return JsonResponse(
                {"error": "PIN incorrecto.", "code": "invalid_pin"}, status=403
            )

        return JsonResponse({"id": supervisor.id, "full_name": supervisor.full_name})


# ─── Item: assignable employees ───────────────────────────────────────────────

class ItemAssignableEmployeesView(SharedTerminalAPIView):
    def get(self, request, item_id):
        item = get_object_or_404(ProductionItem, id=item_id, company=self.company)
        item_stages = {
            _uc.normalize("NFC", s) for s in (item.planning or {}).keys()
        }
        assigned_ids = set(item.assigned_employees.values_list("id", flat=True))

        employees = Employee.objects.filter(
            company=self.company,
            can_work_on_production=True,
            is_active=True,
        ).order_by("last_name", "first_name")

        result = []
        for emp in employees:
            workable = sorted(
                s for s in item_stages if _employee_can_work_stage(emp, s)
            )
            if not workable:
                continue
            result.append({
                "id": emp.id,
                "full_name": emp.full_name,
                "is_assigned": emp.id in assigned_ids,
                "workable_stages": workable,
            })

        return JsonResponse({"employees": result})


# ─── Supervisor: force-leave an employee from a stage ────────────────────────

class SupervisorLeaveEmployeeStageView(SharedTerminalAPIView):
    def post(self, request):
        body = json.loads(request.body or b"{}")
        supervisor_id = body.get("supervisor_id")
        employee_id = body.get("employee_id")
        item_id = body.get("item_id")
        stage_name = body.get("stage_name", "")

        try:
            Employee.objects.get(
                id=supervisor_id,
                company=self.company,
                is_production_supervisor=True,
                is_active=True,
            )
        except Employee.DoesNotExist:
            return JsonResponse({"error": "Supervisor no válido."}, status=403)

        employee = get_object_or_404(
            Employee,
            id=employee_id,
            company=self.company,
            can_work_on_production=True,
            is_active=True,
        )
        item = get_object_or_404(ProductionItem, id=item_id, company=self.company)

        stage_name_n = _uc.normalize("NFC", stage_name)
        try:
            count = StagesToSessionsService.leave_activity(
                stage_name=stage_name_n,
                items=[item],
                employee=employee,
                pause_reason=body.get("pause_reason"),
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({"success": True, "count": count})

# ─── Supervisor: start / resume an employee on a stage ───────────────────────

class SupervisorStartEmployeeStageView(SharedTerminalAPIView):
    def post(self, request):
        body = json.loads(request.body or b"{}")
        supervisor_id = body.get("supervisor_id")
        employee_id = body.get("employee_id")
        item_id = body.get("item_id")
        stage_name = body.get("stage_name", "")

        try:
            Employee.objects.get(
                id=supervisor_id,
                company=self.company,
                is_production_supervisor=True,
                is_active=True,
            )
        except Employee.DoesNotExist:
            return JsonResponse({"error": "Supervisor no válido."}, status=403)

        employee = get_object_or_404(
            Employee,
            id=employee_id,
            company=self.company,
            can_work_on_production=True,
            is_active=True,
        )
        item = get_object_or_404(ProductionItem, id=item_id, company=self.company)

        stage_name_n = _uc.normalize("NFC", stage_name)
        try:
            count = StagesToSessionsService.start_activity(
                stage_name=stage_name_n,
                items=[item],
                employee=employee,
                skip_worker_check=True,
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

        if count == 0:
            return JsonResponse(
                {"error": "No se pudo activar la etapa (puede que ya esté activa o completada)."},
                status=400,
            )
        return JsonResponse({"success": True, "count": count})


# ─── Supervisor: update configuration notes ────────────────────────────────────

class SupervisorUpdateConfigNotesView(SharedTerminalAPIView):
    def post(self, request, config_id):
        body = json.loads(request.body or b"{}")
        supervisor_id = body.get("supervisor_id")
        notes = str(body.get("notes", "")).strip()

        try:
            Employee.objects.get(
                id=supervisor_id,
                company=self.company,
                is_production_supervisor=True,
                is_active=True,
            )
        except Employee.DoesNotExist:
            return JsonResponse({"error": "Supervisor no válido."}, status=403)

        config = get_object_or_404(
            Configuration,
            id=config_id,
            company=self.company,
        )
        config.notes = notes or None
        config.save(update_fields=["notes"])

        return JsonResponse({
            "success": True,
            "config_id": config.id,
            "config_notes": config.notes or "",
        })


# ─── Stage timeline (traceability) ────────────────────────────────────────────

class StageTimelineView(SharedTerminalAPIView):
    """
    Chronology of a stage on one item: who started / resumed / paused /
    finished it and when, plus every comment with its timestamp.
    """

    def get(self, request, item_id, stage_name):
        item = get_object_or_404(ProductionItem, id=item_id, company=self.company)
        stage_name_n = _uc.normalize("NFC", stage_name)

        sessions = [
            s
            for s in ProductionItemSession.objects.filter(production_item=item)
            .select_related("created_by")
            .order_by("start_time")
            if s.start_time and _uc.normalize("NFC", s.production_stage) == stage_name_n
        ]

        raw = {}
        for k, v in (item.planning or {}).items():
            if _uc.normalize("NFC", k) == stage_name_n:
                raw = v if isinstance(v, dict) else {}
                break
        stage_status = (raw.get("status") or "pending") if isinstance(raw, dict) else "pending"

        events = _stage_events(sessions, stage_status)

        now = timezone.now()
        comments: list[dict] = []
        total_seconds = 0

        for sess in sessions:
            emp_name = sess.created_by.full_name if sess.created_by else "—"
            if sess.status != ProductionItemSessionStatus.CANCELLED:
                total_seconds += int(((sess.end_time or now) - sess.start_time).total_seconds())

            for entry in sess.comment_entries or []:
                if not isinstance(entry, dict):
                    continue
                text = (entry.get("text") or "").strip()
                if text:
                    comments.append({
                        "employee_name": emp_name,
                        "comment": text,
                        "at": entry.get("at"),
                    })

        return JsonResponse({
            "stage": stage_name,
            "status": stage_status,
            "events": events,
            "comments": comments,
            "total_seconds": total_seconds,
        })


# ─── Item: assign / unassign employee ────────────────────────────────────────

class ItemAssignEmployeeView(SharedTerminalAPIView):
    def post(self, request, item_id):
        body = json.loads(request.body or b"{}")
        supervisor_id = body.get("supervisor_id")
        employee_id = body.get("employee_id")
        action = body.get("action")

        if action not in ("add", "remove"):
            return JsonResponse(
                {"error": "action debe ser 'add' o 'remove'"}, status=400
            )

        try:
            Employee.objects.get(
                id=supervisor_id,
                company=self.company,
                is_production_supervisor=True,
                is_active=True,
            )
        except Employee.DoesNotExist:
            return JsonResponse({"error": "Supervisor no válido."}, status=403)

        item = get_object_or_404(ProductionItem, id=item_id, company=self.company)

        try:
            employee = Employee.objects.get(
                id=employee_id,
                company=self.company,
                can_work_on_production=True,
                is_active=True,
            )
        except Employee.DoesNotExist:
            return JsonResponse({"error": "Empleado no encontrado."}, status=404)

        if action == "add":
            item.assigned_employees.add(employee)
        else:
            item.assigned_employees.remove(employee)

        return JsonResponse({"success": True})
