from rest_framework import serializers
from company.models import WorkingZone
from assets.models import Asset, AssetIdentifier, AssetPlanningRequestDraft, Client


class WorkingZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkingZone
        fields = ["id", "name"]


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = ["id", "name"]


class MachineAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = ["id", "name"]


class AssetPlanningRequestDraftSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source="client.name", read_only=True, default="")
    asset_name = serializers.CharField(source="asset.name", read_only=True, default="")
    machine_name = serializers.CharField(source="machine.name", read_only=True, default="")
    to_zone_name = serializers.CharField(source="to_zone.name", read_only=True, default="")
    from_zone_name = serializers.CharField(source="from_zone.name", read_only=True, default="")
    service_type_display = serializers.CharField(source="get_service_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    urgency_display = serializers.CharField(source="get_urgency_display", read_only=True, default="")
    missing_fields = serializers.SerializerMethodField()

    class Meta:
        model = AssetPlanningRequestDraft
        fields = [
            "id", "service_type", "service_type_display",
            "client", "client_name",
            "new_client_name", "new_client_tax_id", "new_client_fiscal_address",
            "to_zone", "to_zone_name", "destination_text", "destination_city",
            "from_zone", "from_zone_name", "origin_text", "origin_city",
            "needed_date", "needed_time",
            "urgency", "urgency_display", "notes",
            "cuba_operation_type", "cuba_size",
            "asset", "asset_name", "machine", "machine_name", "load_description",
            "status", "status_display", "missing_fields",
            "resulting_request_id", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "resulting_request_id", "created_at", "updated_at"]

    def get_missing_fields(self, obj):
        return obj.missing_fields_for_submit()

    def to_internal_value(self, data):
        # The app always sends needed_time, using "" for "not set" — TimeField
        # only accepts None for that, not an empty string. Mutate a copy
        # in place rather than spreading into a plain dict — spreading a
        # QueryDict (multipart requests) loses its multi-value semantics and
        # confuses DRF's HTML-input detection for the other fields.
        if data.get("needed_time") == "":
            data = data.copy()
            data["needed_time"] = None
        return super().to_internal_value(data)

    def _current(self, attrs, field_name):
        if field_name in attrs:
            return attrs[field_name]
        return getattr(self.instance, field_name, None) if self.instance else None

    def validate(self, attrs):
        # A draft points at a real client/zone XOR suggests a new one, never
        # both. Which side wins must be decided from what was actually SENT
        # this request (attrs), not the merged/instance state — falling back
        # to the instance here would make a partial update that only sends
        # new_client_name never clear the client it's replacing.
        if attrs.get("client"):
            attrs["new_client_name"] = ""
            attrs["new_client_tax_id"] = ""
            attrs["new_client_fiscal_address"] = ""
        elif (attrs.get("new_client_name") or "").strip():
            attrs["client"] = None

        if attrs.get("to_zone"):
            attrs["destination_text"] = ""
        elif (attrs.get("destination_text") or "").strip():
            attrs["to_zone"] = None

        if attrs.get("from_zone"):
            attrs["origin_text"] = ""
        elif (attrs.get("origin_text") or "").strip():
            attrs["from_zone"] = None

        # Whether the *final* state (merging this update onto the instance)
        # is actually valid does need the instance fallback, since a partial
        # update that doesn't touch client/new_client_name at all must still
        # see whichever one is already set.
        company = self.context.get("company")
        client = self._current(attrs, "client")
        new_client_name = (self._current(attrs, "new_client_name") or "").strip()
        if client:
            if company and client.company_id != company.id:
                raise serializers.ValidationError({"client": "Valid client is required."})
        elif not new_client_name:
            raise serializers.ValidationError(
                {"client": "Either a client or a new client name is required."}
            )

        return attrs


class AssetIdentifierSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetIdentifier
        fields = ["label", "value"]


class AssetWithIdentifiersSerializer(serializers.ModelSerializer):
    identifiers = AssetIdentifierSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = ["id", "name", "identifiers", "data"]