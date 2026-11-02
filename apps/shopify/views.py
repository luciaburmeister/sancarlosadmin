import json
import logging
import re

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.shopify.exceptions import ShopifyAuthError, ShopifyError
from apps.shopify.forms import QuoteRequestForm
from apps.shopify.models import QuoteRequestMode, ShopifyStore
from apps.shopify.services.oauth import ShopifyOAuthService
from apps.shopify.services.quote_requests import QuoteRequestError, ShopifyQuoteRequestService
from apps.shopify.services.webhooks import ShopifyWebhookService
from core.admin.company_access import get_user_companies
from core.views.email_event_webhook import RejectedWebhookThrottle

logger = logging.getLogger(__name__)


class ShopifyWebhookRejectionThrottle(RejectedWebhookThrottle):
    scope = "shopify_webhook_rejections"


@method_decorator(csrf_exempt, name="dispatch")
class ShopifyWebhookView(View):
    """
    Public endpoint for every store's webhook deliveries.

    The store is identified by the shop domain header and must be authenticated
    with its app's client secret before anything is read or saved; the company
    then comes from that store, never from the payload. Verified deliveries are
    saved to the inbox and processed in the background.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        throttle = ShopifyWebhookRejectionThrottle()
        if not throttle.allow_request(request, self):
            return JsonResponse({"error": "Too many invalid requests"}, status=429)

        domain = request.headers.get("X-Shopify-Shop-Domain", "").lower()
        store = ShopifyStore.objects.select_related("app").filter(shop_domain=domain).first()
        secret = store.app.get_client_secret() if store and store.app_id else ""
        if not ShopifyWebhookService.is_authentic(
            request.body, request.headers.get("X-Shopify-Hmac-Sha256", ""), secret
        ):
            throttle.record_rejection()
            logger.warning("Rejected Shopify webhook claiming to be from %s", domain or "(no domain)")
            return JsonResponse({"error": "Invalid signature"}, status=401)

        if not store.is_active:
            return JsonResponse({"status": "ignored"})
        if not request.headers.get("X-Shopify-Webhook-Id"):
            return JsonResponse({"error": "Missing webhook id"}, status=400)
        try:
            payload = json.loads(request.body)
        except ValueError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        event, created = ShopifyWebhookService.receive(store, request.headers, payload)
        if created:
            ShopifyWebhookService.enqueue(event.pk)
        return JsonResponse({"status": "received" if created else "duplicate"})


class ShopifyProxyRejectionThrottle(RejectedWebhookThrottle):
    scope = "shopify_proxy_rejections"


@method_decorator(csrf_exempt, name="dispatch")
class ShopifyQuoteRequestView(View):
    """
    Quote requests from a store's product pages, through Shopify's app proxy.

    Shopify signs the query string with the store's app secret, which stands
    in for CSRF protection; the company comes from that store.
    """

    def post(self, request: HttpRequest) -> JsonResponse:
        throttle = ShopifyProxyRejectionThrottle()
        if not throttle.allow_request(request, self):
            return JsonResponse({"error": "Too many invalid requests"}, status=429)
        domain = request.GET.get("shop", "").lower()
        store = ShopifyStore.objects.select_related("app", "company").filter(shop_domain=domain).first()
        secret = store.app.get_client_secret() if store and store.app_id else ""
        if not ShopifyQuoteRequestService.is_authentic(request.GET, secret):
            throttle.record_rejection()
            logger.warning("Rejected Shopify quote request claiming to be from %s", domain or "(no domain)")
            return JsonResponse({"error": "Invalid signature"}, status=401)
        if not store.is_active or store.quote_requests == QuoteRequestMode.OFF:
            return JsonResponse({"error": "Quote requests are not available"}, status=404)

        form = QuoteRequestForm(request.POST)
        if not form.is_valid():
            return JsonResponse({"errors": form.errors}, status=400)
        if form.cleaned_data["website"]:
            return JsonResponse({"status": "received"})  # a bot: answered, never saved
        try:
            ShopifyQuoteRequestService.receive(store, form.cleaned_data, request.GET.get("logged_in_customer_id", ""))
        except QuoteRequestError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"status": "received"})


class ShopifyOAuthCallbackView(LoginRequiredMixin, View):
    """
    Where Shopify sends the staff member back after they approve the app.

    Outside the language-prefixed URLs because each app must allow this exact
    address. The attempt must have been started by this same session, for a
    store in the user's companies, and Shopify's signature must match the
    store's app.
    """

    login_url = reverse_lazy("admin:login")

    def get(self, request: HttpRequest) -> HttpResponseRedirect:
        if not (request.user.is_staff and request.user.has_perm("shopify.change_shopifystore")):
            raise PermissionDenied
        params = request.GET.dict()
        store_id = ShopifyOAuthService.take_pending_store_id(request.session, params.get("state", ""))
        if store_id is None:
            messages.error(request, _("This Shopify connection attempt is invalid or expired. Start it again."))
            return redirect("admin:shopify_shopifystore_changelist")

        store = get_object_or_404(
            ShopifyStore.objects.select_related("app").filter(company__in=get_user_companies(request.user)),
            pk=store_id,
        )
        change_url = reverse("admin:shopify_shopifystore_change", args=[store.pk])
        shop = params.get("shop", "")
        if (
            store.app_id is None
            or shop != store.shop_domain
            or not re.match(ShopifyStore.SHOP_DOMAIN_RE, shop)
            or not ShopifyOAuthService.is_valid_callback(params, store.app.get_client_secret())
        ):
            messages.error(request, _("Shopify's reply could not be verified. The store was not connected."))
            return redirect(change_url)

        try:
            ShopifyOAuthService.complete_connection(store, params.get("code", ""))
        except ShopifyAuthError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("%(store)s is now connected.") % {"store": store.name})
            try:
                ShopifyWebhookService.sync_subscriptions(store)
            except ShopifyError as exc:
                messages.warning(
                    request,
                    _("Webhooks could not be set up: %(error)s Use \"Sync webhooks\" to try again.")
                    % {"error": exc},
                )
        return redirect(change_url)
