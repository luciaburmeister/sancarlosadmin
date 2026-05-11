"""Endpoints for ProjectFile (a.k.a. expedientes)."""

import json

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from management.models import ProjectFile
from sales.models import Client

from ._serializers import serialize_configuration, serialize_project_file
from ._utils import company_or_error, parse_date_or_error


_MONTHS_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def project_file_list_create_view(request):
    company, error = company_or_error(request)
    if error:
        return error

    if request.method == "GET":
        status_filter = request.GET.get("status", "open")  # open | completed | all
        search = request.GET.get("search", "").strip()
        try:
            page_num = max(1, int(request.GET.get("page", 1)))
        except ValueError:
            page_num = 1

        _PAGE_SIZE = 20

        base_qs = ProjectFile.objects.filter(company=company)
        client_id = request.GET.get("client_id")
        if client_id:
            try:
                base_qs = base_qs.filter(client_id=int(client_id))
            except (ValueError, TypeError):
                pass
        if search:
            base_qs = base_qs.filter(
                Q(name__icontains=search) | Q(client__name__icontains=search)
            )

        open_count = base_qs.exclude(status__in=["completed", "cancelled"]).count()
        completed_count = base_qs.filter(status="completed").count()

        if status_filter == "open":
            filtered_qs = base_qs.exclude(status__in=["completed", "cancelled"])
        elif status_filter == "completed":
            filtered_qs = base_qs.filter(status="completed")
        else:
            filtered_qs = base_qs

        filtered_qs = filtered_qs.prefetch_related("quotes").order_by("-created_at")

        paginator = Paginator(filtered_qs, _PAGE_SIZE)
        page = paginator.get_page(page_num)

        return JsonResponse({
            "results": [serialize_project_file(pf, include_total=True) for pf in page],
            "count": paginator.count,
            "open_count": open_count,
            "completed_count": completed_count,
            "page": page_num,
            "has_next": page.has_next(),
        })

    body = json.loads(request.body)
    client_id = body.get("client_id")
    name = body.get("name", "").strip()

    try:
        client = Client.objects.get(id=client_id, company=company)
    except Client.DoesNotExist:
        return JsonResponse({"error": "Client not found"}, status=404)

    start_date, err = parse_date_or_error(body.get("date"))
    if err:
        return err
    if not name:
        if start_date:
            name = f"{client.name} - {_MONTHS_ES[start_date.month - 1]} {start_date.year}"
        else:
            name = client.name

    project_file = ProjectFile.objects.create(
        company=company, client=client, name=name, start_date=start_date,
    )
    return JsonResponse(serialize_project_file(project_file), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def project_file_detail_view(request, project_file_id):
    company, error = company_or_error(request)
    if error:
        return error

    try:
        pf = ProjectFile.objects.prefetch_related(
            "quotes__configurations__param_values__parameter",
            "quotes__configurations__product",
            "sales_invoices",
            "production_tickets",
        ).get(id=project_file_id, company=company)
    except ProjectFile.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    if request.method == "DELETE":
        pf.delete()
        return JsonResponse({"success": True})

    if request.method == "PATCH":
        body = json.loads(request.body)
        if "status" in body:
            pf.status = body["status"]
            pf.save(update_fields=["status"])
        return JsonResponse(serialize_project_file(pf))

    data = serialize_project_file(pf)
    data["configurations"] = [
        serialize_configuration(config)
        for quote in pf.quotes.all()
        for config in quote.configurations.all()
    ]
    data["sales_invoices"] = [
        {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "date_issued": inv.date_issued.strftime("%Y-%m-%d") if inv.date_issued else None,
            "total_amount": float(inv.total_amount.amount) if inv.total_amount else 0.0,
            "status": inv.status,
        }
        for inv in pf.sales_invoices.all()
    ]
    data["production_tickets"] = [
        {
            "id": t.id,
            "status": t.status,
            "last_stage_label": t.last_stage_label,
            "created_at": t.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for t in pf.production_tickets.all()
    ]
    return JsonResponse(data)
