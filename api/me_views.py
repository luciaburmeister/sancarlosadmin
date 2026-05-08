"""Endpoints related to the authenticated user / employee profile."""

from django.db.models import Q
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from production.models import ProductionItem

from ._serializers import active_ticket_items, serialize_stages
from ._utils import company_or_error


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me_view(request):
    try:
        employee = request.user.employee_profile
    except Exception:
        return JsonResponse({"error": "User has no employee profile linked."}, status=403)

    first_name = (employee.first_name or "").strip()
    last_name = (employee.last_name or "").strip()
    full_name = " ".join([p for p in [first_name, last_name] if p]).strip()

    return JsonResponse({
        "employee_first_name": first_name or None,
        "employee_last_name": last_name or None,
        "employee_full_name": full_name or None,
        "username": request.user.username,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_tasks_view(request):
    """Return production stages assigned to the authenticated employee that are not yet done.

    One result entry per Configuration (ticket source) where the employee has at least one
    non-done stage. Includes the next pending/in_progress stage and progress counters.
    """
    company, error = company_or_error(request)
    if error:
        return error

    try:
        employee = request.user.employee_profile
    except Exception:
        return JsonResponse({"error": "User has no employee profile linked."}, status=403)

    search = request.GET.get("search", "").strip()

    items = (
        ProductionItem.objects.filter(
            ticket__company=company,
            assigned_employees=employee,
        )
        .select_related(
            "ticket__project_file__client",
        )
        .prefetch_related(
            "ticket__items__assigned_employees",
        )
    )

    # Group by ticket, deduplicate
    tickets_seen = {}
    for item in items:
        ticket = item.ticket
        if ticket.id not in tickets_seen:
            tickets_seen[ticket.id] = ticket

    results = []
    for ticket in tickets_seen.values():
        # Resolve configuration and project_file
        config = ticket.source_object
        if config is None:
            continue

        active_items = active_ticket_items(ticket)

        project_file = ticket.project_file
        # Fallback: navigate via config.quote.project_file
        if project_file is None:
            try:
                project_file = config.quote.project_file
            except Exception:
                project_file = None

        # Apply search filter on expedition data
        if search and project_file:
            name_match = search.lower() in (project_file.name or "").lower()
            client_name = project_file.client.name if project_file.client else ""
            client_match = search.lower() in client_name.lower()
            if not name_match and not client_match:
                continue

        # Get all stages (ordered) and filter to those assigned to this employee
        all_stages = serialize_stages(ticket, items=active_items)
        my_stages = [
            s for s in all_stages
            if any(e["id"] == employee.id for e in s["assigned_employees"])
        ]

        if not my_stages:
            continue

        my_stages_total = len(my_stages)
        my_stages_done = sum(1 for s in my_stages if s["status"] == "done")

        # Skip tickets where all my stages are done
        if my_stages_done == my_stages_total:
            continue

        # First non-done stage
        next_stage = next(
            (s for s in my_stages if s["status"] != "done"),
            my_stages[0],
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

        results.append({
            "config_id": config.id,
            "config_name": getattr(config, "name", None) or "",
            "template_name": config.product.name if getattr(config, "product", None) else None,
            "ticket_status": ticket.status,
            "expediente": expediente_data,
            "next_stage": {
                "name": next_stage["name"],
                "status": next_stage["status"],
            },
            "my_stages_total": my_stages_total,
            "my_stages_done": my_stages_done,
        })

    return JsonResponse({"results": results, "count": len(results)})
