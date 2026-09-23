"""Input serializers for the shared production terminal endpoints."""

from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from api.production.permissions import get_employee_with_perm
from production.constants import ProductionPermissions


class ReorderItemsSerializer(serializers.Serializer):
    """Manual priority order dragged in the terminal; first item is most urgent. Needs "company" in context."""

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
                perm=ProductionPermissions.REORDER_ITEMS,
            )
        except PermissionDenied:
            raise serializers.ValidationError("No tienes permiso para esta acción.")
        return value


class SupervisorPermissionSerializer(serializers.Serializer):
    """Base for scheduling-board serializers requiring an authorized supervisor. Needs "company" in context."""

    supervisor_id = serializers.IntegerField()

    def validate_supervisor_id(self, value):
        try:
            get_employee_with_perm(
                company=self.context["company"],
                employee_id=value,
                perm=ProductionPermissions.RESCHEDULE_SESSIONS,
            )
        except PermissionDenied as e:
            detail = e.detail.get("error") if isinstance(e.detail, dict) else e.detail
            raise serializers.ValidationError(detail)
        return value


class RescheduleSessionSerializer(SupervisorPermissionSerializer):
    """Drag-drop move or time-sheet edit; omitting start_time/end_time auto-snaps to the first free slot."""

    date = serializers.DateField()
    employee_id = serializers.IntegerField(required=False, allow_null=True)
    start_time = serializers.TimeField(required=False, allow_null=True)
    end_time = serializers.TimeField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True)


class CreateSessionFromBacklogSerializer(RescheduleSessionSerializer):
    """Create a planned session from a backlog stage; employee_id is mandatory here."""

    production_ticket_id = serializers.IntegerField()
    production_stage = serializers.CharField(max_length=50)
    employee_id = serializers.IntegerField()


class ReorderDaySessionsSerializer(SupervisorPermissionSerializer):
    """Re-sequence one employee's planned sessions for one day; times are repacked to match."""

    employee_id = serializers.IntegerField()
    date = serializers.DateField()
    session_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
