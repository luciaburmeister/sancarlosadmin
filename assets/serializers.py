from rest_framework import serializers
from company.models import WorkingZone
from assets.models import Asset, AssetIdentifier, Client


class WorkingZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkingZone
        fields = ["id", "name"]


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = ["id", "name"]


class AssetIdentifierSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetIdentifier
        fields = ["label", "value"]


class AssetWithIdentifiersSerializer(serializers.ModelSerializer):
    identifiers = AssetIdentifierSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = ["id", "name", "identifiers", "data"]