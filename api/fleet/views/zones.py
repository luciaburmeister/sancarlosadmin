from assets.models import Asset, Client
from assets.serializers import AssetWithIdentifiersSerializer, ClientSerializer, WorkingZoneSerializer
from company.models import WorkingZone, ConfigurationList
from company.utils import get_current_employee
from django.core.cache import cache
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

CACHE_TIMEOUT = 60 * 30  # 30 minutes


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

        zone_type = self.request.query_params.get("zone_type", "all")
        cache_key = f"zones_{employee.company_id}_{zone_type}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        queryset = WorkingZone.objects.filter(company=employee.company, is_active=True)
        cache.set(cache_key, queryset, timeout=CACHE_TIMEOUT)
        return queryset


class ClientsListView(generics.ListAPIView):
    serializer_class = ClientSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.OrderingFilter]
    ordering = ["name"]

    def get_queryset(self):
        employee = get_current_employee(self.request.user)
        if employee is None or employee.company_id is None:
            return Client.objects.none()

        cache_key = f"clients_{employee.company_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        queryset = Client.objects.filter(company=employee.company)
        cache.set(cache_key, queryset, timeout=CACHE_TIMEOUT)
        return queryset


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

        cache_key = f"identifiers_{employee.company_id}_{label}_{asset_type_param}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response({"assets": cached})

        asset_types = [t.strip() for t in asset_type_param.split(",")]
        assets = Asset.objects.prefetch_related("identifiers").filter(
            asset_type__name__in=asset_types,
            company=employee.company,
            identifiers__label__iexact=label,
        ).distinct()

        serializer = AssetWithIdentifiersSerializer(assets, many=True)
        cache.set(cache_key, serializer.data, timeout=CACHE_TIMEOUT)
        return Response({"assets": serializer.data})


class ConfigListView(APIView):
    """
    Generic endpoint to fetch a configuration list by key.

    Query parameters:
      - key (required): the configuration key, e.g. cuba_material_types

    Example:
      GET /api/config/?key=cuba_material_types
      GET /api/config/?key=implementos
      GET /api/config/?key=tipos_camion
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None or employee.company_id is None:
            return Response({"values": []})

        key = request.query_params.get("key", "").strip()
        if not key:
            return Response({"error": "key query parameter is required"}, status=400)

        cache_key = f"config_{employee.company_id}_{key}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response({"key": key, "values": cached})

        try:
            config = ConfigurationList.objects.get(
                company=employee.company,
                key=key
            )
            cache.set(cache_key, config.values, timeout=CACHE_TIMEOUT)
            return Response({"key": key, "values": config.values})
        except ConfigurationList.DoesNotExist:
            return Response({"key": key, "values": []})