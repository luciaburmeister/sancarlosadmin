from django.http import JsonResponse

from api._utils import CompanyAPIView
from configurator.models import ProductTemplate


class ProductTemplateListView(CompanyAPIView):
    def get(self, request):
        templates = ProductTemplate.objects.filter(
            company=self.company, show_in_configurator=True
        )
        data = []
        for t in templates:
            params = list(t.parameters.order_by("order").values(
                "id", "name", "label", "type", "options", "order", "parameter_group", "required"
            ))
            data.append({"id": t.id, "name": t.name, "parameters": params})
        return JsonResponse({"templates": data})
