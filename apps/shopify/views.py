import json
import logging
import re
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import FormView
from unfold.views import UnfoldModelAdminViewMixin

from apps.shopify.exceptions import ShopifyAuthError, ShopifyBusy, ShopifyError
from apps.shopify.forms import WebStoreAccessForm, QuoteRequestForm
from apps.shopify.models import ShopifyStore
from apps.shopify.services.app_proxy import ShopifyAppProxy
from apps.shopify.services.customer_quotes import CustomerQuoteError, CustomerQuoteService, CustomerQuoteSummary
from apps.shopify.services.quote_payments import QuotePaymentError, ShopifyQuotePaymentService
from apps.shopify.services.discounts import ShopifyDiscountService
from apps.shopify.services.oauth import ShopifyOAuthService
from apps.shopify.services.quote_requests import QuoteRequestError, ShopifyQuoteRequestService
from apps.shopify.services.webhooks import ShopifyWebhookService
from core.admin.company_access import get_user_companies
from core.views.email_event_webhook import RejectedWebhookThrottle
from sales.documents import render_quote_pdf
from sales.models import Client, Quote

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


class ShopifyAppProxyMixin:
    """
    For views a store's pages call through Shopify's app proxy.

    Shopify signs the query string with the store's app secret, which stands
    in for CSRF protection and authenticates the store and the signed-in
    customer (``ShopifyAppProxy``). ``self.store`` is that store, so the
    company always comes from it; ``self.customer_id`` is the customer, empty
    for a guest. ``check_access`` adds a view's own conditions.
    """

    store: ShopifyStore
    customer_id: str

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        throttle = ShopifyProxyRejectionThrottle()
        if not throttle.allow_request(request, self):
            return JsonResponse({"error": "Too many invalid requests"}, status=429)
        domain = request.GET.get("shop", "").lower()
        store = ShopifyStore.objects.select_related("app", "company").filter(shop_domain=domain).first()
        secret = store.app.get_client_secret() if store and store.app_id else ""
        if not ShopifyAppProxy.is_authentic(request.GET, secret):
            throttle.record_rejection()
            logger.warning("Rejected Shopify app proxy call claiming to be from %s", domain or "(no domain)")
            return JsonResponse({"error": "Invalid signature"}, status=401)
        if not store.is_active:
            return JsonResponse({"error": "Not available"}, status=404)
        self.store = store
        self.customer_id = ShopifyAppProxy.customer_id(request.GET)
        denied = self.check_access(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def check_access(self, request: HttpRequest) -> JsonResponse | None:
        """A response refusing the call, or None to go on."""
        return None


@method_decorator(csrf_exempt, name="dispatch")
class ShopifyQuoteRequestView(ShopifyAppProxyMixin, View):
    """Quote requests from a store's product and quote list pages."""

    def check_access(self, request: HttpRequest) -> JsonResponse | None:
        if not self.store.takes_quote_requests:
            return JsonResponse({"error": "Quote requests are not available"}, status=404)
        return None

    def post(self, request: HttpRequest) -> JsonResponse:
        store = self.store
        form = QuoteRequestForm(request.POST)
        if not form.is_valid():
            return JsonResponse({"errors": form.errors}, status=400)
        if form.cleaned_data["website"]:
            return JsonResponse({"status": "received"})  # a bot: answered, never saved
        try:
            ShopifyQuoteRequestService.receive(store, form.cleaned_data, self.customer_id)
        except QuoteRequestError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"status": "received"})


class CustomerQuotesMixin(ShopifyAppProxyMixin):
    """The store's quotes area: a signed-in customer with web store access; ``self.client`` is their client."""

    client: Client

    def check_access(self, request: HttpRequest) -> JsonResponse | None:
        if not self.store.shows_customer_quotes:
            return JsonResponse({"error": "Not available"}, status=404)
        if not self.customer_id:
            return JsonResponse({"error": "Sign in to see your quotes"}, status=401)
        client = CustomerQuoteService.client_of(self.store, self.customer_id)
        if client is None:
            return JsonResponse({"error": "No web store access"}, status=403)
        self.client = client
        return None


class CustomerQuoteListView(CustomerQuotesMixin, View):
    def get(self, request: HttpRequest) -> JsonResponse:
        quotes = CustomerQuoteService.quotes(self.store, self.client)
        return JsonResponse({"quotes": [CustomerQuoteSummary.of(self.store, quote).as_dict() for quote in quotes]})


class CustomerQuotePdfView(CustomerQuotesMixin, View):
    """The quote's PDF, as staff download it from the admin; only once it has a price."""

    def get(self, request: HttpRequest, quote_id: int) -> HttpResponse:
        try:
            quote = CustomerQuoteService.priced_quote(self.store, self.client, quote_id)
        except Quote.DoesNotExist:
            raise Http404
        pdf = render_quote_pdf(quote.pk)
        if not pdf:
            raise Http404
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="Presupuesto_{quote.pk}.pdf"'
        return response


@method_decorator(csrf_exempt, name="dispatch")
class CustomerQuoteAcceptView(CustomerQuotesMixin, View):
    def post(self, request: HttpRequest, quote_id: int) -> JsonResponse:
        try:
            quote = CustomerQuoteService.accept(self.store, self.client, quote_id)
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
        except CustomerQuoteError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"quote": CustomerQuoteSummary.of(self.store, quote).as_dict()})


@method_decorator(csrf_exempt, name="dispatch")
class CustomerQuotePayView(CustomerQuotesMixin, View):
    """Where the customer pays an accepted quote: Shopify's checkout of its draft order."""

    def post(self, request: HttpRequest, quote_id: int) -> JsonResponse:
        try:
            quote = CustomerQuoteService.accepted_quote(self.store, self.client, quote_id)
            url = ShopifyQuotePaymentService.checkout_url(self.store, quote, self.customer_id)
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
        except QuotePaymentError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except ShopifyBusy as exc:
            return JsonResponse({"error": str(exc)}, status=409)
        except ShopifyError:
            logger.exception("Shopify could not prepare the payment of quote %s", quote_id)
            return JsonResponse({"error": _("The payment could not be prepared. Please try again later.")}, status=502)
        return JsonResponse({"url": url})


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


class GiveWebStoreAccessView(UnfoldModelAdminViewMixin, FormView):
    """Staff choose the clients whose Shopify customers get their client's discounts.

    A view of its own rather than an admin form: creating Shopify customers
    must not happen inside the admin's database transaction.
    """

    title = _("Give clients web store access")
    permission_required = ("shopify.change_shopifystore",)
    template_name = "admin/shopify/give_web_store_access.html"
    form_class = WebStoreAccessForm

    def get_store(self) -> ShopifyStore:
        # The admin's queryset: only stores of the user's companies.
        return get_object_or_404(self.model_admin.get_queryset(self.request), pk=self.kwargs["pk"])

    def get_form_kwargs(self) -> dict:
        return {**super().get_form_kwargs(), "company_id": self.get_store().company_id}

    def get_context_data(self, **kwargs: Any) -> dict:
        store = self.get_store()
        return super().get_context_data(
            **kwargs, store=store, back_url=reverse("admin:shopify_shopifystore_change", args=[store.pk])
        )

    def form_valid(self, form: WebStoreAccessForm) -> HttpResponseRedirect:
        store = self.get_store()
        try:
            result = ShopifyDiscountService.give_web_store_access(store, form.cleaned_data["clients"])
        except ShopifyError as exc:
            messages.error(self.request, str(exc))
            return self.form_invalid(form)
        if result.given:
            messages.success(self.request, ngettext(
                "%(count)s client can now sign in and get their discounts.",
                "%(count)s clients can now sign in and get their discounts.",
                len(result.given),
            ) % {"count": len(result.given)})
        for error in result.errors:
            messages.warning(self.request, error)
        if not store.sync_client_discounts:
            messages.info(self.request, _("Turn on \"Offer client discounts\" for the store to publish them."))
        return redirect(reverse("admin:shopify_shopifycustomerlink_changelist") + f"?store__id__exact={store.pk}")
