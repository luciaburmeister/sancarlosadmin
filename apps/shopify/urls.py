from django.urls import path, re_path

from apps.shopify.views import (
    CustomerQuoteAcceptView,
    CustomerQuoteListView,
    CustomerQuotePayView,
    CustomerQuotePdfView,
    ShopifyOAuthCallbackView,
    ShopifyQuoteRequestView,
    ShopifyWebhookView,
)

urlpatterns = [
    path("oauth/callback/", ShopifyOAuthCallbackView.as_view(), name="shopify_oauth_callback"),
    path("webhooks/", ShopifyWebhookView.as_view(), name="shopify_webhook"),
    # Through the app proxy, which forwards the path without a trailing slash.
    re_path(r"^proxy/quote-request/?$", ShopifyQuoteRequestView.as_view(), name="shopify_quote_request"),
    re_path(r"^proxy/quotes/?$", CustomerQuoteListView.as_view(), name="shopify_customer_quotes"),
    re_path(
        r"^proxy/quotes/(?P<quote_id>\d+)/pdf/?$", CustomerQuotePdfView.as_view(), name="shopify_customer_quote_pdf"
    ),
    re_path(
        r"^proxy/quotes/(?P<quote_id>\d+)/accept/?$",
        CustomerQuoteAcceptView.as_view(),
        name="shopify_customer_quote_accept",
    ),
    re_path(r"^proxy/quotes/(?P<quote_id>\d+)/pay/?$", CustomerQuotePayView.as_view(), name="shopify_customer_quote_pay"),
]
