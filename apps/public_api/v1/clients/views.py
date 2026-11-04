from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from apps.public_api.pagination import PublicPagination
from apps.public_api.views import PublicAPIView
from sales.models import Client

from .serializers import ClientFilterSerializer, ClientSerializer

TAG = "Clients"


class ClientListView(PublicAPIView):
    required_permissions = {"GET": ["sales.view_client"]}

    @extend_schema(
        tags=[TAG],
        summary="List clients",
        parameters=[
            ClientFilterSerializer,
            OpenApiParameter("page", int, description="Page number (1-based)."),
            OpenApiParameter("page_size", int, description="Results per page (default 50, max 100)."),
        ],
        responses=ClientSerializer(many=True),
    )
    def get(self, request):
        filters = ClientFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        f = filters.validated_data

        qs = Client.objects.filter(company=self.company)
        if tax_identifier := f.get("tax_identifier", "").strip():
            qs = qs.filter(tax_identifier__iexact=tax_identifier)
        if search := f.get("search", "").strip():
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(commercial_name__icontains=search)
                | Q(tax_identifier__icontains=search)
            )

        paginator = PublicPagination()
        page = paginator.paginate_queryset(qs.order_by("name", "id"), request, view=self)
        return paginator.get_paginated_response(ClientSerializer(page, many=True).data)


class ClientDetailView(PublicAPIView):
    required_permissions = {"GET": ["sales.view_client"]}

    @extend_schema(tags=[TAG], summary="Get a client", responses=ClientSerializer)
    def get(self, request, client_id):
        client = Client.objects.filter(company=self.company, pk=client_id).first()
        if client is None:
            raise NotFound("Client not found.")
        return Response(ClientSerializer(client).data)
