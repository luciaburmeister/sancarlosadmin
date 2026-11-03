"""
Quote requests sent from a store's pages: one or more products, any of them
made to measure.

The storefront posts them through Shopify's app proxy (``/apps/<subpath>/
quote-request`` on the store's own domain; see ``ShopifyAppProxy``). A
request becomes a draft quote for the requester's client, priced by the ERP
like a quote made by hand (made-to-measure products as configurations, priced
by the configurator), and is recorded as the store's ``ShopifyQuote``; staff
take it from there with the ERP's normal quote flow.

Storefront side, per store: set up the app's proxy in the Dev Dashboard
(prefix ``apps``, a subpath, proxy URL ``<ERP>/integrations/shopify/proxy``)
and have the theme post ``QuoteRequestForm``'s fields to it. The catalog push
tags products ``request-quote`` (show the form) and ``quote-only`` (also hide
the price and checkout), and publishes made-to-measure parameters as the
variant field ``polipus.made_to_measure``.
"""

from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.shopify.models import ShopifyCustomerLink, ShopifyQuote, ShopifyStore, VariantLinkStatus
from apps.shopify.services.base import ShopifyService
from inventory.choices import OnlineSaleMode
from sales.models import Quote
from sales.services.external_order import ExternalCustomer, ExternalOrderError, ExternalOrderService, QuoteRequestLine


class QuoteRequestError(Exception):
    """The request can't become a quote; the message is safe to show the customer."""


class ShopifyQuoteRequestService:
    # Shopify signs each proxied request as it forwards it, so a valid one is always fresh.
    @classmethod
    @transaction.atomic
    def receive(cls, store: ShopifyStore, data: dict, customer_id: str = "") -> Quote:
        """The draft quote for a validated request (``QuoteRequestForm`` data).

        ``customer_id`` is the signed-in customer's Shopify ID (``gid://…``),
        empty for a guest. All or nothing: a line that can't be quoted leaves
        no quote and no new client.
        """
        from apps.shopify.services.orders import ShopifyOrderService

        lines = cls._lines(store, data["lines"])
        # Always the requester's own client, whatever the store does for orders:
        # the quote is sent back to them. A signed-in customer already linked
        # (by staff or an order) keeps that client; otherwise the client comes
        # from the details typed in, as for a guest, and the customer is not
        # linked to it: anyone can type a client's email.
        linked = customer_id and ShopifyCustomerLink.objects.filter(store=store, customer_id=customer_id).exists()
        client = ShopifyOrderService.customer_client(
            store,
            customer_id if linked else None,
            ExternalCustomer(name=data["name"], email=data["email"], phone=data["phone"]),
        )
        name = ShopifyService.erp_text(
            gettext_noop("Web quote request: %(product)s")
            if len(lines) == 1
            else gettext_noop("Web quote request: %(product)s and %(count)s more"),
            product=lines[0].item.name,
            count=len(lines) - 1,
        )
        try:
            quote = ExternalOrderService.request_quote(store.company, client, lines, name=name, notes=data["notes"])
        except ExternalOrderError as exc:
            raise QuoteRequestError(str(exc)) from exc
        ShopifyQuote.objects.create(store=store, quote=quote, customer_id=customer_id)
        return quote

    @staticmethod
    def _lines(store: ShopifyStore, data: list[dict]) -> list[QuoteRequestLine]:
        """The requested lines, refused unless each product may be quoted in this store, as asked."""
        variant_ids = {ShopifyService.gid("ProductVariant", line["variant_id"]) for line in data}
        links = {
            link.variant_id: link
            for link in store.variant_links.filter(
                variant_id__in=variant_ids, status=VariantLinkStatus.MATCHED, item__sell_online=True
            ).select_related("item")
        }
        lines = []
        for line in data:
            link = links.get(ShopifyService.gid("ProductVariant", line["variant_id"]))
            if link is None:
                raise QuoteRequestError(_("This product can't be quoted."))
            made_to_measure = store.sells_made_to_measure(link.item)
            if made_to_measure != (line["parameters"] is not None):
                raise QuoteRequestError(
                    _("Choose the measurements of %(product)s.") % {"product": link.item.name}
                    if made_to_measure
                    else _("%(product)s can't be made to measure.") % {"product": link.item.name}
                )
            if store.sale_mode(link.item) == OnlineSaleMode.BUY:
                raise QuoteRequestError(_("This product can't be quoted."))
            lines.append(QuoteRequestLine(link.item, line["quantity"], line["parameters"], line["notes"]))
        return lines
