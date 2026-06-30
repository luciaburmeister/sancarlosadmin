"""
Endpoints for the shared production terminal.

All views use SharedTerminalAPIView (requires can_use_shared_terminal on CompanyUser).
Mutations require the target employee's 4-digit work_pin in the request body.
"""

import json
import unicodedata as _uc

from django.contrib.auth.hashers import check_password
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

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
            ]
        })


# ─── Task list for a given employee ──────────────────────────────────────────


def _employee_can_work_stage(employee: Employee, stage_name_nfc: str) -> bool:
    allowed = [_uc.normalize("NFC", s) for s in (employee.production_stages or [])]
    return not allowed or stage_name_nfc in allowed


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
                pairs.append({
                    "sort": (ii, jj),
                    "production_item_id": item.id,
                    "stage_name": stage_name,
                    "status": status,
                    "component_label": component_label,
                    "is_assigned": item.id in assigned_item_ids,
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
            has_my_session = any(w["id"] == employee.id for w in workers)
            results.append({
                **base,
                "next_stage": {
                    "name": stage_p["stage_name"],
                    "status": stage_p["status"],
                    "production_item_id": stage_p["production_item_id"],
                    "component_label": stage_p["component_label"],
                    "active_workers": workers,
                    "has_my_open_session": has_my_session,
                    "started_by_others": (
                        stage_p["status"] == "in_progress" and bool(workers) and not has_my_session
                    ),
                    "is_assigned": stage_p["is_assigned"],
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
    Subclasses implement `_run(stage_name, item, employee) -> int`.
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
            count = self._run(stage_name=stage_name_n, item=item, employee=employee)
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

    def _run(self, *, stage_name: str, item, employee: Employee) -> int:
        raise NotImplementedError


class TerminalStageStartView(_TerminalStageActionView):
    def _run(self, *, stage_name, item, employee):
        # skip_worker_check=True: terminal allows starting a new task
        # even if the employee forgot to close a previous session.
        return StagesToSessionsService.start_activity(
            stage_name=stage_name,
            items=[item],
            employee=employee,
            skip_worker_check=True,
        )


class TerminalStageLeaveView(_TerminalStageActionView):
    def _run(self, *, stage_name, item, employee):
        return StagesToSessionsService.leave_activity(
            stage_name=stage_name,
            items=[item],
            employee=employee,
        )


class TerminalStageFinishView(_TerminalStageActionView):
    def _run(self, *, stage_name, item, employee):
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
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({"success": True, "count": count})


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
