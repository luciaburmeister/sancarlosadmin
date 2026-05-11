from rest_framework import serializers
from company.models import WorkingZone
from assets.models import Client


class WorkingZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkingZone
        fields = ["id", "name"]


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = ["id", "name"]