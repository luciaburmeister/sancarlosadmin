from rest_framework import serializers
from .models import AssetTransaction, AssetPlanning, TransactionTypeChoices


class AssetTransactionSerializer(serializers.ModelSerializer):
    # These are set automatically by the view, not sent by Android
    asset = serializers.PrimaryKeyRelatedField(read_only=True)
    company = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = AssetTransaction
        fields = [
            'id',
            'asset',
            'company',
            'from_zone',
            'to_zone',
            'transaction_type',
            'date',
            'notes',
            'firma',     # ← the signature field
        ]


class AssetPlanningDeliverSerializer(serializers.ModelSerializer):
    """
    Used only for the delivery action — updates the planning status.
    """
    class Meta:
        model = AssetPlanning
        fields = ['id', 'status']
        read_only_fields = ['id']