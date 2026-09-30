"""Endpoints for managing production stages per ProductionItem (BOM line)."""

import json
import unicodedata as _uc

from django.core.exceptions import ValidationError
from django.http import JsonResponse

from api._serializers import (
    active_ticket_items,
    get_ticket_for_config,
    serialize_configuration_stages_context,
    serialize_production_items_with_stages,
)
from api._utils import CompanyAPIView
from company.models import Employee
from management.models import ProjectFileStatus
from production.models import ProductionItemStatus, ProductionTicket, ProductionTicketStatus
from production.exceptions import StageHasActiveWorkersError
from production.services.stages_to_sessions import StagesToSessionsService
from production.services.ticket_cancel import cancel_production_ticket


def _payload(ticket, items, config, employee=None):
    return {
        "ticket_status": ticket.status,
        "production_items": serialize_production_items_with_stages(ticket, items=items, employee=employee),
        "configuration": serialize_configuration_stages_context(config),
    }


def _item_or_error(items, item_id):
    for it in items:
        if it.id == item_id:
            return it
    return None


def _planning_stage_keys(item):
    return {_uc.normalize("NFC", k) for k in (item.planning or {}).keys()}


def _ticket_mutation_blocked(ticket):
    if ticket.status == ProductionTicketStatus.CANCELLED:
        return JsonResponse(
            {
                "error": "Este ticket de producción está cancelado.",
                "code": "ticket_cancelled",
            },
            status=409,
        )
    pf = ticket.project_file
    if pf is not None and pf.status == ProjectFileStatus.CANCELLED:
        return JsonResponse(
            {
                "error": "El expediente está cancelado.",
                "code": "expediente_cancelled",
            },
            status=409,
        )
    return None


class ConfigurationStagesView(CompanyAPIView):
    def get(self, request, config_id):
        config, ticket, err = get_ticket_for_config(config_id, self.company)
        if err:
            return err

        if ticket is None:
            return JsonResponse(
                {"error": "No production ticket found. Convert the configuration first."},
                status=404,
            )

        items = active_ticket_items(ticket)
        return JsonResponse(_payload(ticket, items, config, employee=self.employee))


class ConfigurationCancelTicketView(CompanyAPIView):
    def post(self, request, config_id):
        config, ticket, err = get_ticket_for_config(config_id, self.company)
        if err:
            return err

        if ticket is None:
            return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

        blocked = _ticket_mutation_blocked(ticket)
        if blocked:
            return blocked

        cancel_production_ticket(ticket)

        ticket = ProductionTicket.objects.prefetch_related(
            "items__assigned_employees"
        ).get(pk=ticket.pk)
        items = active_ticket_items(ticket)
        return JsonResponse({"success": True, **_payload(ticket, items, config, employee=self.employee)})


class StageAssignItemView(CompanyAPIView):
    def post(self, request, config_id, item_id, stage_name):
        config, ticket, err = get_ticket_for_config(config_id, self.company)
        if err:
            return err

        if ticket is None:
            return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

        blocked = _ticket_mutation_blocked(ticket)
        if blocked:
            return blocked

        stage_name = _uc.normalize("NFC", stage_name)

        body = json.loads(request.body or b"{}")
        employee_ids = body.get("employee_ids", [])
        try:
            want = sorted({int(x) for x in employee_ids})
        except (TypeError, ValueError):
            return JsonResponse({"error": "employee_ids must be a list of integers."}, status=400)

        employees = list(Employee.objects.filter(id__in=want, company=self.company))
        if len(employees) != len(want):
            return JsonResponse({"error": "One or more employees are invalid for this company."}, status=400)

        items = active_ticket_items(ticket)
        item = _item_or_error(items, item_id)
        if item is None:
            return JsonResponse({"error": "Production item not found for this ticket."}, status=404)

        if stage_name not in _planning_stage_keys(item):
            return JsonResponse({"error": "Stage not defined for this production item."}, status=404)

        item.assigned_employees.set(employees)

        ticket = ProductionTicket.objects.prefetch_related(
            "items__assigned_employees"
        ).get(pk=ticket.pk)
        items = active_ticket_items(ticket)
        return JsonResponse({"success": True, **_payload(ticket, items, config, employee=self.employee)})


class StageStartItemView(CompanyAPIView):
    def post(self, request, config_id, item_id, stage_name):
        employee = self.employee
        employee_stages = [_uc.normalize("NFC", s) for s in (employee.production_stages or [])]
        stage_name_n = _uc.normalize("NFC", stage_name)
        if employee_stages and stage_name_n not in employee_stages:
            return JsonResponse(
                {
                    "error": "No estás autorizado para iniciar esta etapa.",
                    "code": "not_authorized_for_stage",
                },
                status=403,
            )

        config, ticket, err = get_ticket_for_config(config_id, self.company)
        if err:
            return err

        if ticket is None:
            return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

        blocked = _ticket_mutation_blocked(ticket)
        if blocked:
            return blocked

        items = active_ticket_items(ticket)
        item = _item_or_error(items, item_id)
        if item is None:
            return JsonResponse({"error": "Production item not found for this ticket."}, status=404)

        if stage_name_n not in _planning_stage_keys(item):
            return JsonResponse({"error": "Stage not defined for this production item."}, status=404)

        if not item.assigned_employees.exists():
            item.assigned_employees.add(employee)

        try:
            count = StagesToSessionsService.start_activity(
                stage_name=stage_name_n,
                items=[item],
                employee=employee,
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

        ticket = ProductionTicket.objects.prefetch_related(
            "items__assigned_employees"
        ).get(pk=ticket.pk)
        items = active_ticket_items(ticket)
        return JsonResponse(
            {
                "started": count,
                "ticket_status": ticket.status,
                **_payload(ticket, items, config, employee=employee),
            }
        )


class StageRevertItemView(CompanyAPIView):
    def post(self, request, config_id, item_id, stage_name):
        config, ticket, err = get_ticket_for_config(config_id, self.company)
        if err:
            return err

        if ticket is None:
            return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

        blocked = _ticket_mutation_blocked(ticket)
        if blocked:
            return blocked

        stage_name_n = _uc.normalize("NFC", stage_name)

        body = json.loads(request.body) if request.body else {}
        target_status = body.get("target", "in_progress")

        items = active_ticket_items(ticket)
        item = _item_or_error(items, item_id)
        if item is None:
            return JsonResponse({"error": "Production item not found for this ticket."}, status=404)

        if stage_name_n not in _planning_stage_keys(item):
            return JsonResponse({"error": "Stage not defined for this production item."}, status=404)

        try:
            if Employee.can_reopen_production_stages(request.user, ticket.company_id):
                count = StagesToSessionsService.revert_activity(
                    stage_name=stage_name_n,
                    items=[item],
                    target_status=target_status,
                    user=request.user,
                    employee=self.employee,
                )
            elif target_status == ProductionItemStatus.IN_PROGRESS:
                count = StagesToSessionsService.undo_finish(
                    stage_name=stage_name_n, items=[item], employee=self.employee
                )
            else:
                return JsonResponse(
                    {
                        "error": "Solo los supervisores de producción pueden reabrir etapas.",
                        "code": "not_authorized_to_reopen_stage",
                    },
                    status=403,
                )
        except ValidationError as e:
            return JsonResponse({"error": " ".join(e.messages)}, status=400)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

        ticket = ProductionTicket.objects.prefetch_related(
            "items__assigned_employees"
        ).get(pk=ticket.pk)
        items = active_ticket_items(ticket)
        return JsonResponse(
            {
                "reverted": count,
                "ticket_status": ticket.status,
                **_payload(ticket, items, config, employee=self.employee),
            }
        )


class StageLeaveItemView(CompanyAPIView):
    def post(self, request, config_id, item_id, stage_name):
        employee = self.employee
        config, ticket, err = get_ticket_for_config(config_id, self.company)
        if err:
            return err

        if ticket is None:
            return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

        blocked = _ticket_mutation_blocked(ticket)
        if blocked:
            return blocked

        stage_name_n = _uc.normalize("NFC", stage_name)

        items = active_ticket_items(ticket)
        item = _item_or_error(items, item_id)
        if item is None:
            return JsonResponse({"error": "Production item not found for this ticket."}, status=404)

        if stage_name_n not in _planning_stage_keys(item):
            return JsonResponse({"error": "Stage not defined for this production item."}, status=404)

        try:
            count = StagesToSessionsService.leave_activity(
                stage_name=stage_name_n,
                items=[item],
                employee=employee,
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

        ticket = ProductionTicket.objects.prefetch_related(
            "items__assigned_employees"
        ).get(pk=ticket.pk)
        items = active_ticket_items(ticket)
        return JsonResponse(
            {
                "left": count,
                "ticket_status": ticket.status,
                **_payload(ticket, items, config, employee=employee),
            }
        )


class StageFinishItemView(CompanyAPIView):
    def post(self, request, config_id, item_id, stage_name):
        employee = self.employee
        config, ticket, err = get_ticket_for_config(config_id, self.company)
        if err:
            return err

        if ticket is None:
            return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

        blocked = _ticket_mutation_blocked(ticket)
        if blocked:
            return blocked

        stage_name_n = _uc.normalize("NFC", stage_name)

        items = active_ticket_items(ticket)
        item = _item_or_error(items, item_id)
        if item is None:
            return JsonResponse({"error": "Production item not found for this ticket."}, status=404)

        if stage_name_n not in _planning_stage_keys(item):
            return JsonResponse({"error": "Stage not defined for this production item."}, status=404)

        try:
            count = StagesToSessionsService.finish_activity(
                stage_name=stage_name_n,
                items=[item],
                employee=employee,
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

        ticket = ProductionTicket.objects.prefetch_related(
            "items__assigned_employees"
        ).get(pk=ticket.pk)
        items = active_ticket_items(ticket)
        return JsonResponse(
            {
                "finished": count,
                "ticket_status": ticket.status,
                **_payload(ticket, items, config, employee=employee),
            }
        )
