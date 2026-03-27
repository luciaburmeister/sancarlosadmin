from django.shortcuts import render
from rest_framework import generics, permissions
from company.models import LocationRecord
from company.serializers import LocationRecordSerializer

class LocationRecordCreateView(generics.CreateAPIView):
    serializer_class = LocationRecordSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        employee = self.request.user.employee_profile
        serializer.save(
            employee=employee,
            company=employee.company
        )