from rest_framework import serializers
from assets.models import TransactionTypeChoices


class AssetTransactionSerializer(serializers.Serializer):
    """
    Validates the body of POST /assets/<id>/transaction/.

    By declaring transaction_type as a ChoiceField pointing at
    TransactionTypeChoices, DRF automatically rejects any value that
    is not in the choices list — no hardcoded strings needed in the view.
    """
    transaction_type = serializers.ChoiceField(
        choices=TransactionTypeChoices.choices
    )
    # GPS coordinates — used by movement and delivery
    latitude = serializers.FloatField(required=False, default=0)
    longitude = serializers.FloatField(required=False, default=0)

    # Firma and imagen — used by movement and delivery
    firma = serializers.CharField(required=False, default="", allow_blank=True)
    imagen = serializers.CharField(required=False, default="", allow_blank=True)

    # Albaran fields — used by movement and delivery
    # No default so they are excluded from validated_data when not sent
    matricula = serializers.CharField(required=False, allow_blank=True)
    tipo_camion = serializers.CharField(required=False, allow_blank=True)
    implemento = serializers.CharField(required=False, allow_blank=True)
    cliente = serializers.CharField(required=False, allow_blank=True)
    obra = serializers.CharField(required=False, allow_blank=True)
    trabajo = serializers.CharField(required=False, allow_blank=True)
    tipo_cuba = serializers.CharField(required=False, allow_blank=True)
    m3 = serializers.CharField(required=False, allow_blank=True)
    vertedero = serializers.CharField(required=False, allow_blank=True)

    # Receipt-specific fields — no default so they are excluded from
    # validated_data when not sent (prevents them polluting movement/delivery data)
    nro_albaran = serializers.CharField(required=False, allow_blank=True)
    foto_albaran = serializers.CharField(required=False, allow_blank=True)
    peso_neto = serializers.CharField(required=False, allow_blank=True)
    material_vertido = serializers.CharField(required=False, allow_blank=True)

    # Zone override — sent when driver picks from the multiple-zones dialog
    zone_id = serializers.IntegerField(required=False, default=None, allow_null=True)