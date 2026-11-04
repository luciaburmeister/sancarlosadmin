from decimal import Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


class TaxTypeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField(source="tax_name")
    country = serializers.CharField(help_text="ISO 3166-1 alpha-2.")
    behavior = serializers.ChoiceField(
        choices=[("add", "add"), ("subtract", "subtract")],
        help_text="`add` (e.g. VAT) adds to the total; `subtract` (a retention, e.g. IRPF) subtracts.",
    )
    rates = serializers.SerializerMethodField()
    default_rate = serializers.DecimalField(source="default_percentage", max_digits=5, decimal_places=2)

    @extend_schema_field(
        serializers.ListField(
            child=serializers.DecimalField(max_digits=5, decimal_places=2),
            help_text="Allowed rates. Empty: any rate is accepted.",
        )
    )
    def get_rates(self, tax_type):
        return [
            str(Decimal(str(rate)).quantize(Decimal("0.01")))
            for rate in sorted(Decimal(str(option)) for option in (tax_type.percentage_options or []))
        ]


class TaxTypeFilterSerializer(serializers.Serializer):
    country = serializers.CharField(required=False, max_length=2, help_text="ISO 3166-1 alpha-2, e.g. ES.")
