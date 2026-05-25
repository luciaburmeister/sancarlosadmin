from django.urls import path

from .views.product_templates import ProductTemplateListView

urlpatterns = [
    path("product-templates/", ProductTemplateListView.as_view(), name="product_template_list"),
]
