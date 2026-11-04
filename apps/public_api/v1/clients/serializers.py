from rest_framework import serializers

from invoices.models import PaymentMethod
from company.models import DocumentLanguage


class ClientSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField(help_text="Legal name.")
    commercial_name = serializers.CharField(allow_blank=True)
    tax_identifier = serializers.CharField(allow_blank=True, help_text="Tax id (CIF/NIF/VAT number).")
    email = serializers.EmailField(allow_blank=True)
    phone = serializers.CharField(allow_blank=True)
    address = serializers.CharField(allow_blank=True)
    language = serializers.ChoiceField(
        choices=DocumentLanguage.choices, allow_blank=True, help_text="Language of the documents sent to the client.",
    )
    payment_method = serializers.ChoiceField(
        choices=PaymentMethod.choices,
        help_text="Default for new invoices.",
    )
    payment_terms_days = serializers.IntegerField(help_text="Days from issue to due date.")
    payment_days = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="Days of the month the client pays on: due dates move to the next of them.",
    )
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class ClientFilterSerializer(serializers.Serializer):
    search = serializers.CharField(required=False, help_text="Name, commercial name or tax id (contains).")
    tax_identifier = serializers.CharField(required=False, help_text="Exact tax id (case-insensitive).")
