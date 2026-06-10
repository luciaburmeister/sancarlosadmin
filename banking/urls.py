"""Banking URL configuration — public callback endpoints."""
from django.urls import path

from banking.views.callback_views import ArcopayCallbackView, ArcopayErrorCallbackView

urlpatterns = [
    path(
        "arcopay/callback/",
        ArcopayCallbackView.as_view(),
        name="arcopay_callback",
    ),
    path(
        "arcopay/callback-error/",
        ArcopayErrorCallbackView.as_view(),
        name="arcopay_callback_error",
    ),
]
