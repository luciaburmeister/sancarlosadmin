from django.urls import path

from .views.clients import ClientDetailView, ClientListCreateView, TermsAndConditionsView

urlpatterns = [
    path("clients/", ClientListCreateView.as_view(), name="client_list_create"),
    path("clients/<int:client_id>/", ClientDetailView.as_view(), name="client_detail"),
    path("terms/", TermsAndConditionsView.as_view(), name="terms_and_conditions"),
]
