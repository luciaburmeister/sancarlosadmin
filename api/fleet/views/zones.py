from assets.models import Asset, Client
from assets.serializers import AssetWithIdentifiersSerializer, ClientSerializer, WorkingZoneSerializer
from company.models import WorkingZone
from company.utils import get_current_employee
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class ZonesListView(generics.ListAPIView):
    serializer_class = WorkingZoneSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["zone_type"]
    ordering = ["name"]

    def get_queryset(self):
        employee = get_current_employee(self.request.user)
        if employee is None or employee.company_id is None:
            return WorkingZone.objects.none()
        return WorkingZone.objects.filter(company=employee.company, is_active=True)


class ClientsListView(generics.ListAPIView):
    serializer_class = ClientSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering = ["name"]

    def get_queryset(self):
        employee = get_current_employee(self.request.user)
        if employee is None or employee.company_id is None:
            return Client.objects.none()
        return Client.objects.filter(company=employee.company)


class AssetIdentifiersView(APIView):
    """
    Returns assets with their identifiers, filtered by label and asset type.

    Query parameters:
      - label      (required): identifier label to look up, e.g. MATRICULA
      - asset_type (required): comma-separated asset type names, e.g. CAMION,CAMIONES

    Example calls:
      /api/identifiers/?label=MATRICULA&asset_type=CAMION,CAMIONES
      /api/identifiers/?label=SERIAL&asset_type=MAQUINAS
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None or employee.company_id is None:
            return Response({"identifiers": []})

        label = request.query_params.get("label", "").strip().upper()
        asset_type_param = request.query_params.get("asset_type", "").strip()

        if not label or not asset_type_param:
            return Response(
                {"error": "label and asset_type query parameters are required"},
                status=400
            )

        asset_types = [t.strip() for t in asset_type_param.split(",")]

        assets = Asset.objects.prefetch_related("identifiers").filter(
            asset_type__name__in=asset_types,
            company=employee.company,
            identifiers__label__iexact=label,
        ).distinct()

        serializer = AssetWithIdentifiersSerializer(assets, many=True)
        return Response({"assets": serializer.data})