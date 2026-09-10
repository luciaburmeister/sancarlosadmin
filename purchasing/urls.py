from django.urls import path

from .views.quote_public import QuotePublicResponseView

urlpatterns = [
    path(
        "responder/<str:token>/",
        QuotePublicResponseView.as_view(),
        name="purchasing_quote_public_response",
    ),
]