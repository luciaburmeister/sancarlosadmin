"""Input serializers for the shared production terminal endpoints."""

from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from api.production.permissions import REORDER_ITEMS_PERM, get_employee_with_perm


class ReorderItemsSerializer(serializers.Serializer):
    """Manual priority order dragged in the terminal; first item is most urgent.

    Requires "company" in the serializer context to validate the acting employee.
    """

    employee_id = serializers.IntegerField()
    item_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
    )

    def validate_employee_id(self, value):
        try:
            get_employee_with_perm(
                company=self.context["company"],
                employee_id=value,
                perm=REORDER_ITEMS_PERM,
            )
        except PermissionDenied:
            raise serializers.ValidationError("No tienes permiso para esta acción.")
        return value
