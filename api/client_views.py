"""CRUD endpoints for sales.Client (with search + create)."""

import json

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from sales.models import Client

from ._serializers import serialize_client
from ._utils import company_or_error


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def client_list_create_view(request):
    company, error = company_or_error(request)
    if error:
        return error

    if request.method == "GET":
        search = request.GET.get("search", "").strip()
        clients = Client.objects.filter(company=company).order_by("name")
        if search:
            clients = clients.filter(name__icontains=search)
        return JsonResponse({"clients": [serialize_client(c) for c in clients]})

    body = json.loads(request.body)
    name = body.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "Name is required"}, status=400)

    client = Client.objects.create(
        company=company,
        name=name,
        phone=body.get("phone", ""),
        email=body.get("email", ""),
    )
    return JsonResponse(serialize_client(client), status=201)
