from django.urls import path, re_path

from apps.shopify.views import ShopifyOAuthCallbackView, ShopifyQuoteRequestView, ShopifyWebhookView

urlpatterns = [
    path("oauth/callback/", ShopifyOAuthCallbackView.as_view(), name="shopify_oauth_callback"),
    path("webhooks/", ShopifyWebhookView.as_view(), name="shopify_webhook"),
    # Through the app proxy, which forwards the path without a trailing slash.
    re_path(r"^proxy/quote-request/?$", ShopifyQuoteRequestView.as_view(), name="shopify_quote_request"),
]
