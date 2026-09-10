from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.views import View
from django.http import JsonResponse
from django.db.models import Q

from materials.models import Provider


@method_decorator(staff_member_required, name="dispatch")
class ProviderAutocompleteView(View):
    """
    Responds to the AJAX search performed by the multiple-autocomplete
    widget used when picking providers to send a Quote Request to.
    Mirrors the pattern used by CommissionistAgentAutocompleteView.
    """

    def get(self, request, *args, **kwargs):
        term = request.GET.get("term", "").strip()
        queryset = Provider.objects.all().order_by("name")
        if term:
            queryset = queryset.filter(Q(name__icontains=term))

        results = [{"id": provider.pk, "text": provider.name} for provider in queryset[:50]]
        return JsonResponse({"results": results, "pagination": {"more": False}})