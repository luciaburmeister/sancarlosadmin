from .models import LocationRecord
from rest_framework import serializers

class LocationRecordSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(
        source='employee.full_name',
        read_only=True
    )
    employee = serializers.PrimaryKeyRelatedField(
        read_only=True  # ✅ set by the view automatically, not required from Android
    )

    class Meta:
        model = LocationRecord
        fields = ['id', 'employee', 'employee_name', 'latitude', 'longitude', 'recorded_at']