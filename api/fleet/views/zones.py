from assets.models import Client
from assets.serializers import ClientSerializer, WorkingZoneSerializer
from company.models import WorkingZone
from company.utils import get_current_employee
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics
from rest_framework.permissions import IsAuthenticated


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
