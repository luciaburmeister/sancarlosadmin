"""CRUD endpoints for sales.Client (with search + create)."""

import hashlib
import json

from django.core.paginator import Paginator
from django.http import JsonResponse

from api._serializers import serialize_client
from api._utils import CompanyAPIView
from sales.models import Client
from sales.services.client_terms import apply_terms_acceptance
from sales.services.client_terms_email_service import ClientTermsEmailService

_ALLOWED_PAYMENT_METHODS = {"transfer", "cash", "card"}
_OPEN_PROJECT_STATUSES = {"new", "active", "on_hold"}


class ClientListCreateView(CompanyAPIView):
    def get(self, request):
        company = self.company
        search = request.GET.get("search", "").strip()
        payment_method = request.GET.get("payment_method", "").strip()
        has_active_project = request.GET.get("has_active_project", "").strip().lower()
        clients = Client.objects.filter(company=company).order_by("name")

        if payment_method:
            if payment_method not in _ALLOWED_PAYMENT_METHODS:
                return JsonResponse(
                    {"error": f"Invalid payment_method: {payment_method}"},
                    status=400,
                )
            clients = clients.filter(payment_method=payment_method)

        if has_active_project == "true":
            clients = clients.filter(project_files__status__in=_OPEN_PROJECT_STATUSES)
        elif has_active_project == "false":
            clients = clients.exclude(project_files__status__in=_OPEN_PROJECT_STATUSES)

        if search:
            clients = clients.filter(name__icontains=search)
        clients = clients.distinct().order_by("name")

        try:
            page_num = max(1, int(request.GET.get("page", 1)))
        except ValueError:
            page_num = 1
        try:
            raw_size = int(request.GET.get("page_size", 25))
        except ValueError:
            raw_size = 25
        page_size = max(1, min(50, raw_size))

        paginator = Paginator(clients, page_size)
        page = paginator.get_page(page_num)
        return JsonResponse(
            {
                "results": [serialize_client(c) for c in page],
                "count": paginator.count,
                "page": page_num,
                "has_next": page.has_next(),
            }
        )

    def post(self, request):
        company = self.company
        body = json.loads(request.body)
        name = body.get("name", "").strip()
        if not name:
            return JsonResponse({"error": "Name is required"}, status=400)
        payment_method = (body.get("payment_method") or "transfer").strip()
        if payment_method not in _ALLOWED_PAYMENT_METHODS:
            return JsonResponse(
                {"error": f"Invalid payment_method: {payment_method}"},
                status=400,
            )

        terms_signature = (body.get("terms_signature") or "").strip()

        client = Client.objects.create(
            company=company,
            name=name,
            phone=body.get("phone", ""),
            email=body.get("email", ""),
            payment_method=payment_method,
        )
        if terms_signature:
            apply_terms_acceptance(
                client=client,
                company=company,
                signature_payload=terms_signature,
            )
            client.save(
                update_fields=[
                    "terms_signature",
                    "accepted_terms_company",
                    "accepted_terms_text",
                    "terms_accepted_at",
                ]
            )
            ClientTermsEmailService.queue_terms_acceptance_email(client)
        return JsonResponse(serialize_client(client), status=201)


class ClientDetailView(CompanyAPIView):
    def patch(self, request, client_id):
        company = self.company
        try:
            client = Client.objects.get(pk=client_id, company=company)
        except Client.DoesNotExist:
            return JsonResponse({"error": "Client not found"}, status=404)

        try:
            body = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                return JsonResponse({"error": "Name is required"}, status=400)
            client.name = name
        if "phone" in body:
            client.phone = (body.get("phone") or "") or ""
        if "email" in body:
            client.email = (body.get("email") or "") or ""
        if "payment_method" in body:
            payment_method = (body.get("payment_method") or "").strip()
            if payment_method not in _ALLOWED_PAYMENT_METHODS:
                return JsonResponse(
                    {"error": f"Invalid payment_method: {payment_method}"},
                    status=400,
                )
            client.payment_method = payment_method

        client.save()
        return JsonResponse(serialize_client(client))


class TermsAndConditionsView(CompanyAPIView):
    """Returns the company's current T&C text and its version hash."""

    def get(self, request):
        text = (self.company.terms_text or "").strip()
        version = hashlib.sha256(text.encode()).hexdigest()[:8] if text else ""
        return JsonResponse({"text": text, "version": version})
