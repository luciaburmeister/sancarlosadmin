# production/views/configuration_autocomplete.py

from django.db.models import Q
from unfold.views import BaseAutocompleteView

from configurator.models import Configuration


class ConfigurationAutocompleteView(BaseAutocompleteView):
    model = Configuration
    model_admin = None

    def get_queryset(self):
        qs = super().get_queryset()

        term = self.request.GET.get("term", "").strip()

        if term:
            qs = qs.filter(
                Q(id__icontains=term) |
                Q(name__icontains=term)
            )

        return qs.order_by("-id")