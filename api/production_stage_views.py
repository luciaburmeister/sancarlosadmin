"""Endpoints for managing production stages of a Configuration's ProductionTicket."""

import json

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from company.models import Employee
from production.models import ProductionTicket
from production.services.stages_to_sessions import StagesToSessionsService

from ._serializers import active_ticket_items, get_ticket_for_config, serialize_stages
from ._utils import company_or_error


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def configuration_stages_view(request, config_id):
    company, error = company_or_error(request)
    if error:
        return error

    _, ticket, err = get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse(
            {"error": "No production ticket found. Convert the configuration first."},
            status=404,
        )

    items = active_ticket_items(ticket)
    return JsonResponse({
        "ticket_status": ticket.status,
        "stages": serialize_stages(ticket, items=items),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stage_assign_view(request, config_id, stage_name):
    company, error = company_or_error(request)
    if error:
        return error

    _, ticket, err = get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

    body = json.loads(request.body)
    employee_ids = body.get("employee_ids", [])
    employees = list(Employee.objects.filter(id__in=employee_ids, company=company))

    items = active_ticket_items(ticket)
    for item in items:
        if stage_name in (item.planning or {}):
            item.assigned_employees.set(employees)

    ticket = ProductionTicket.objects.prefetch_related(
        "items__assigned_employees"
    ).get(pk=ticket.pk)

    items = active_ticket_items(ticket)
    return JsonResponse({"success": True, "stages": serialize_stages(ticket, items=items)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stage_start_view(request, config_id, stage_name):
    import unicodedata as _uc

    company, error = company_or_error(request)
    if error:
        return error

    try:
        employee = request.user.employee_profile
    except Exception:
        return JsonResponse({"error": "User has no employee profile linked."}, status=403)

    # Enforce stage authorization: if the employee has specific production stages
    # defined, the requested stage must be among them.
    employee_stages = [_uc.normalize("NFC", s) for s in (employee.production_stages or [])]
    if employee_stages and _uc.normalize("NFC", stage_name) not in employee_stages:
        return JsonResponse(
            {
                "error": "No estás autorizado para iniciar esta etapa.",
                "code": "not_authorized_for_stage",
            },
            status=403,
        )

    _, ticket, err = get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

    items = active_ticket_items(ticket)

    relevant_items = [item for item in items if stage_name in (item.planning or {})]
    any_assigned = any(item.assigned_employees.exists() for item in relevant_items)
    if not any_assigned:
        for item in relevant_items:
            item.assigned_employees.add(employee)

    try:
        count = StagesToSessionsService.start_activity(
            stage_name=stage_name,
            items=items,
            employee=employee,
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    ticket = ProductionTicket.objects.prefetch_related(
        "items__assigned_employees"
    ).get(pk=ticket.pk)

    items = active_ticket_items(ticket)
    return JsonResponse({"started": count, "ticket_status": ticket.status, "stages": serialize_stages(ticket, items=items)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stage_revert_view(request, config_id, stage_name):
    company, error = company_or_error(request)
    if error:
        return error

    _, ticket, err = get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

    body = json.loads(request.body) if request.body else {}
    target_status = body.get("target", "in_progress")

    items = active_ticket_items(ticket)

    try:
        count = StagesToSessionsService.revert_activity(
            stage_name=stage_name,
            items=items,
            target_status=target_status,
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    ticket = ProductionTicket.objects.prefetch_related(
        "items__assigned_employees"
    ).get(pk=ticket.pk)

    items = active_ticket_items(ticket)
    return JsonResponse({"reverted": count, "ticket_status": ticket.status, "stages": serialize_stages(ticket, items=items)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def stage_finish_view(request, config_id, stage_name):
    company, error = company_or_error(request)
    if error:
        return error

    try:
        employee = request.user.employee_profile
    except Exception:
        return JsonResponse({"error": "User has no employee profile linked."}, status=403)

    _, ticket, err = get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

    items = active_ticket_items(ticket)

    try:
        count = StagesToSessionsService.finish_activity(
            stage_name=stage_name,
            items=items,
            employee=employee,
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    ticket = ProductionTicket.objects.prefetch_related(
        "items__assigned_employees"
    ).get(pk=ticket.pk)

    items = active_ticket_items(ticket)
    return JsonResponse({"finished": count, "ticket_status": ticket.status, "stages": serialize_stages(ticket, items=items)})
