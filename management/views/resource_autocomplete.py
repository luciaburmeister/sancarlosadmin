from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.http import JsonResponse
from unfold.views import BaseAutocompleteView

from core.admin.company_access import get_user_companies


class ResourceAutocompleteView(BaseAutocompleteView):
    """
    JSON autocomplete for employees, machines and assets (multi-model GenericFK).
    Extends Unfold's BaseAutocompleteView; overrides get() because the queryset
    spans three different models rather than one.

    Query params: ?term=<search>&company=<pk>
    """

    PLANNABLE = [
        ("company", "Employee", lambda e: f"{e.last_name}, {e.first_name}"),
        ("company", "Machine",  lambda m: m.name),
        ("assets",  "Asset",    lambda a: a.name),
    ]

    def get(self, request, *args, **kwargs):
        from django.apps import apps
        from django.db.models import Q

        term = request.GET.get("term", "").strip()
        company_pk = request.GET.get("company")
        user_companies = get_user_companies(request.user)

        results = []
        for app_label, model_name, label_fn in self.PLANNABLE:
            Model = apps.get_model(app_label, model_name)
            ContentType.objects.get_for_model(Model)  # warm cache
            qs = Model.objects.filter(is_active=True, company__in=user_companies)
            if company_pk:
                qs = qs.filter(company_id=company_pk)
            if term:
                if model_name == "Employee":
                    qs = qs.filter(
                        Q(first_name__icontains=term) | Q(last_name__icontains=term)
                    )
                else:
                    qs = qs.filter(name__icontains=term)

            for obj in qs[:20]:
                results.append({
                    "id": f"{model_name.lower()}:{obj.pk}",
                    "text": label_fn(obj),
                    "type": model_name.lower(),
                })

        return JsonResponse({"results": results})
