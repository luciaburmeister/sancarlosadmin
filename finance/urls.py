from django.urls import path
from .views import export_tax_periods_excel

app_name = "finance"

urlpatterns = [
    path("tax-periods/export/", export_tax_periods_excel, name="tax_periods_export"),
]