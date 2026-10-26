from django.db.models import Q
from django.http import JsonResponse
from django.views.generic import ListView

from unfold.views import BaseAutocompleteView

from assets.models import Asset, AssetIdentifierLabel, AssetTypeCategory
from core.admin.company_access import get_user_companies


class FilteredAssetAutocompleteView(BaseAutocompleteView):
    model = Asset
    exclude_categories = ()
    only_category = None
    plates_only = False

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .filter(is_active=True)
            .select_related("asset_type")
            .prefetch_related("identifiers")
        )
        if not self.request.user.is_superuser:
            qs = qs.filter(company__in=get_user_companies(self.request.user))
        if self.only_category:
            qs = qs.filter(asset_type__category=self.only_category)
        if self.exclude_categories:
            qs = qs.exclude(asset_type__category__in=self.exclude_categories)
        if self.plates_only:
            qs = qs.filter(identifiers__label__iexact=AssetIdentifierLabel.PLATE)
        term = self.request.GET.get("term")
        if term:
            qs = qs.filter(Q(name__icontains=term) | Q(identifiers__value__icontains=term))
        return qs.distinct().order_by("name")

    def label(self, obj):
        if not self.plates_only:
            return str(obj)
        plate = obj.matricula or ""
        if plate:
            return f"{plate} — {obj.name}"
        return str(obj)

    def get(self, request, *args, **kwargs):
        ListView.get(self, request, *args, **kwargs)
        context = self.get_context_data()
        return JsonResponse(
            {
                "results": [
                    {"id": str(obj.pk), "text": self.label(obj)}
                    for obj in context["object_list"]
                ],
                "pagination": {"more": context["page_obj"].has_next()},
            }
        )


class ContainerAutocompleteView(FilteredAssetAutocompleteView):
    exclude_categories = (AssetTypeCategory.TRUCK, AssetTypeCategory.MACHINE)


class TruckPlateAutocompleteView(FilteredAssetAutocompleteView):
    only_category = AssetTypeCategory.TRUCK
    plates_only = True
