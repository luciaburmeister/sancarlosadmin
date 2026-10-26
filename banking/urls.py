"""Banking URL configuration — public callback endpoints."""
from django.urls import path

from banking.views.callback_views import (
    ArcopayCallbackView,
    ArcopayErrorCallbackView,
    ArcopayPaymentCallbackView,
    ArcopayPaymentErrorCallbackView,
)

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
    path(
        "arcopay/payment-callback/",
        ArcopayPaymentCallbackView.as_view(),
        name="arcopay_payment_callback",
    ),
    path(
        "arcopay/payment-callback-error/",
        ArcopayPaymentErrorCallbackView.as_view(),
        name="arcopay_payment_callback_error",
    ),
]
