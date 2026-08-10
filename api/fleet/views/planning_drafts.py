"""Mobile-app draft requests for AssetPlanningRequest (see assets.models.AssetPlanningRequestDraft).

An employee fills these in with free-text hints (no real WorkingZone/ProjectFile
required) and "publishes" one once it has everything its service_type needs. A
planner then reads it in the Django admin and creates the real
AssetPlanningRequest by hand.
"""
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from api._utils import CompanyAPIView
from assets.constants import SERVICE_TYPE_FIELDS
from assets.models import (
    Asset, AssetPlanningRequestDraft, AssetPlanningRequestDraftStatus, AssetTypeCategory,
    CubaOperationType, CubaSize, PlanningUrgency, ServiceType,
)
from assets.serializers import AssetPlanningRequestDraftSerializer, MachineAssetSerializer


def _first_error(errors):
    """Flattens DRF's {field: [msg, ...]} into the flat message the app expects."""
    return str(next(iter(errors.values()))[0])


class PlanningDraftListCreateView(CompanyAPIView):
    def get(self, request):
        drafts = AssetPlanningRequestDraft.objects.filter(
            requested_by=self.employee
        ).select_related("client", "asset", "to_zone", "from_zone").order_by("-created_at")
        return JsonResponse({"results": AssetPlanningRequestDraftSerializer(drafts, many=True).data})

    def post(self, request):
        serializer = AssetPlanningRequestDraftSerializer(
            data=request.data, context={"company": self.company},
        )
        if not serializer.is_valid():
            return JsonResponse({"error": _first_error(serializer.errors)}, status=400)
        serializer.save(company=self.company, requested_by=self.employee)
        return JsonResponse(serializer.data, status=201)


class PlanningDraftDetailView(CompanyAPIView):
    def _get_draft(self, draft_id):
        return get_object_or_404(AssetPlanningRequestDraft, pk=draft_id, requested_by=self.employee)

    def get(self, request, draft_id):
        draft = self._get_draft(draft_id)
        return JsonResponse(AssetPlanningRequestDraftSerializer(draft).data)

    def patch(self, request, draft_id):
        draft = self._get_draft(draft_id)
        if draft.status != AssetPlanningRequestDraftStatus.DRAFT:
            return JsonResponse({"error": "Only drafts can still be edited."}, status=400)
        serializer = AssetPlanningRequestDraftSerializer(
            draft, data=request.data, partial=True, context={"company": self.company},
        )
        if not serializer.is_valid():
            return JsonResponse({"error": _first_error(serializer.errors)}, status=400)
        serializer.save()
        return JsonResponse(serializer.data)

    def delete(self, request, draft_id):
        draft = self._get_draft(draft_id)
        # Cancellable up until a planner has acted on it (PROCESSED).
        if draft.status not in (AssetPlanningRequestDraftStatus.DRAFT, AssetPlanningRequestDraftStatus.SUBMITTED):
            return JsonResponse({"error": "Only pending requests can still be cancelled."}, status=400)
        draft.status = AssetPlanningRequestDraftStatus.CANCELLED
        draft.save(update_fields=["status", "updated_at"])
        return JsonResponse({"success": True})


class PlanningDraftSubmitView(CompanyAPIView):
    def post(self, request, draft_id):
        draft = get_object_or_404(AssetPlanningRequestDraft, pk=draft_id, requested_by=self.employee)
        if draft.status != AssetPlanningRequestDraftStatus.DRAFT:
            return JsonResponse({"error": "Only drafts can be submitted."}, status=400)
        missing = draft.missing_fields_for_submit()
        if missing:
            return JsonResponse({"error": "Missing required fields.", "missing_fields": missing}, status=400)
        draft.status = AssetPlanningRequestDraftStatus.SUBMITTED
        draft.save(update_fields=["status", "updated_at"])
        return JsonResponse(AssetPlanningRequestDraftSerializer(draft).data)


class PlanningDraftServiceTypesView(CompanyAPIView):
    """Static catalog the app needs to know which fields to show per service_type."""

    def get(self, request):
        return JsonResponse({
            "service_types": [
                {
                    "value": value,
                    "label": label,
                    "needs_origin": "from_zone" in SERVICE_TYPE_FIELDS.get(value, set()),
                    "needs_asset": "asset" in SERVICE_TYPE_FIELDS.get(value, set()),
                    "needs_load_description": "load_description" in SERVICE_TYPE_FIELDS.get(value, set()),
                    "needs_cuba_fields": "cuba_operation_type" in SERVICE_TYPE_FIELDS.get(value, set()),
                }
                for value, label in ServiceType.choices
            ],
            "cuba_operation_types": [
                {"value": value, "label": label} for value, label in CubaOperationType.choices
            ],
            "cuba_sizes": [
                {"value": value, "label": label} for value, label in CubaSize.choices
            ],
            "urgencies": [
                {"value": value, "label": label} for value, label in PlanningUrgency.choices
            ],
        })


class MachinesListView(CompanyAPIView):
    """The company's own active machines, for the "own machine transport" draft field."""

    def get(self, request):
        qs = Asset.objects.filter(
            company=self.company, asset_type__category=AssetTypeCategory.MACHINE, is_active=True,
        )
        return JsonResponse({"results": MachineAssetSerializer(qs.order_by("name"), many=True).data})
