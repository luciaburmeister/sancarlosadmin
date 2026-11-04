from drf_spectacular.utils import OpenApiParameter, extend_schema

from apps.public_api.pagination import PublicPagination
from apps.public_api.views import PublicAPIView
from invoices.models import TaxType

from .serializers import TaxTypeFilterSerializer, TaxTypeSerializer


class TaxTypeListView(PublicAPIView):
    """Tax types are shared by every company (they're defined per country)."""

    required_permissions = {"GET": ["invoices.view_taxtype"]}

    @extend_schema(
        tags=["Tax types"],
        summary="List active tax types",
        description="The taxes a sales invoice line can carry (`lines[].taxes[].tax_type_id`).",
        parameters=[
            TaxTypeFilterSerializer,
            OpenApiParameter("page", int, description="Page number (1-based)."),
            OpenApiParameter("page_size", int, description="Results per page (default 50, max 100)."),
        ],
        responses=TaxTypeSerializer(many=True),
    )
    def get(self, request):
        filters = TaxTypeFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)

        qs = TaxType.objects.filter(is_active=True)
        if country := filters.validated_data.get("country"):
            qs = qs.filter(country=country.upper())

        paginator = PublicPagination()
        page = paginator.paginate_queryset(qs.order_by("country", "tax_name", "id"), request, view=self)
        return paginator.get_paginated_response(TaxTypeSerializer(page, many=True).data)
