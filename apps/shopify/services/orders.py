"""
Importing a store's Shopify orders as ERP quotes.

Each order is read again from Shopify (webhooks arrive out of order and carry
old snapshots), translated into ``sales.services.external_order`` terms and
applied with ``ExternalOrderService``, so the ERP keeps exactly what the
customer was charged.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from apps.shopify.client import ShopifyClient
from apps.shopify.enums import AccessScope, OrderFinancialStatus
from apps.shopify.models import ClientMode, FulfillmentBy, ShopifyCustomerLink, ShopifyOrder, ShopifyStore, VariantLinkStatus
from apps.shopify.services.base import ShopifyService
from apps.shopify.services.fulfillments import ShopifyFulfillmentService
from apps.shopify.services.invoicing import ShopifyInvoicingService
from invoices.models import SalesInvoiceStatus
from sales.models import Client, QuoteStatus
from sales.services.external_order import (
    ExternalAddress,
    ExternalCustomer,
    ExternalOrder,
    ExternalOrderLine,
    ExternalOrderService,
)


class ShopifyOrderError(Exception):
    """The order can't be imported yet; the message tells staff what to fix."""


class ShopifyOrderService(ShopifyService):
    AMOUNTS_FIELDS = f"""
      discountAllocations {{ allocatedAmountSet {{ {ShopifyService.MONEY} }} }}
      taxLines {{ ratePercentage priceSet {{ {ShopifyService.MONEY} }} }}
    """
    ADDRESS_FIELDS = "name company address1 address2 city zip province country countryCodeV2 phone"
    # Reading the order's customer needs the read_customers scope; without it
    # the client is matched from the order's email and billing address instead.
    CUSTOMER_FIELDS = "customer { id email firstName lastName phone }"
    CUSTOMER_PLACEHOLDER = "__CUSTOMER__"
    ORDER_QUERY = f"""
    query ($id: ID!) {{
      order(id: $id) {{
        id name createdAt updatedAt cancelledAt displayFinancialStatus taxesIncluded email phone
        currentTotalPriceSet {{ shopMoney {{ amount currencyCode }} }}
        totalPriceSet {{ {ShopifyService.MONEY} }}
        totalRefundedSet {{ {ShopifyService.MONEY} }}
        {CUSTOMER_PLACEHOLDER}
        billingAddress {{ {ADDRESS_FIELDS} }}
        shippingAddress {{ {ADDRESS_FIELDS} }}
        lineItems(first: 250) {{ nodes {{
          id name quantity currentQuantity variant {{ id }}
          originalUnitPriceSet {{ {ShopifyService.MONEY} }}
          {AMOUNTS_FIELDS}
        }} }}
        shippingLines(first: 20) {{ nodes {{
          originalPriceSet {{ {ShopifyService.MONEY} }}
          {AMOUNTS_FIELDS}
        }} }}
        {ShopifyFulfillmentService.FULFILLMENTS_FIELDS}
        {ShopifyInvoicingService.REFUNDS_FIELDS}
      }}
    }}
    """
    UPDATED_ORDERS_QUERY = """
    query ($first: Int!, $after: String, $query: String!) {
      orders(first: $first, after: $after, sortKey: UPDATED_AT, query: $query) {
        nodes { id updatedAt }
        pageInfo { hasNextPage endCursor }
      }
    }
    """

    @classmethod
    def sync_order(cls, store: ShopifyStore, order_id: str) -> ShopifyOrder | None:
        """Create or update the order's quote. ``None`` when the store doesn't import orders."""
        if not store.import_orders:
            return None
        data = ShopifyClient(store).graphql(cls._order_query(store), {"id": order_id})["order"]
        if data is None:
            raise ShopifyOrderError(_("The order no longer exists in Shopify."))

        with transaction.atomic():
            record, _created = ShopifyOrder.objects.get_or_create(store=store, order_id=data["id"])
            # One import per order at a time; concurrent events wait here.
            record = (
                ShopifyOrder.objects.select_for_update(of=("self",)).select_related("quote", "invoice").get(pk=record.pk)
            )
            updated_at = parse_datetime(data["updatedAt"])
            if record.shopify_updated_at == updated_at and not record.error:
                return record  # nothing changed since the last import

            # Once shipped or invoiced, refunds no longer change the quote:
            # they are credited instead (ShopifyInvoicingService).
            locked = record.quote is not None and ExternalOrderService.is_locked(record.quote)
            invoiced = record.invoice is not None and record.invoice.status != SalesInvoiceStatus.CANCELLED

            # The ERP's own pending delivery orders follow the order's changes.
            erp_ships = store.fulfillment_by == FulfillmentBy.ERP
            if data["cancelledAt"]:
                if record.quote is not None:
                    ExternalOrderService.cancel(record.quote, cancel_pending_deliveries=erp_ships)
            else:
                order = cls._external_order(store, data, keep_refunded=locked)
                record.quote = ExternalOrderService.sync_quote(
                    order, record.quote, replace_pending_deliveries=erp_ships
                )
                record.shipping_address = ExternalOrderService.delivery_address(order)
                if record.shipping_address is not None:
                    # A new address before dispatch: the parcel follows it.
                    ExternalOrderService.ship_pending_deliveries_to(record.quote, record.shipping_address)

            total = cls.money(data["totalPriceSet" if locked else "currentTotalPriceSet"])
            record.name = data["name"]
            record.placed_at = parse_datetime(data["createdAt"])
            record.refunded_total = cls.money(data["totalRefundedSet"])
            record.financial_status = data["displayFinancialStatus"] or ""
            record.is_cancelled = bool(data["cancelledAt"])
            record.shopify_updated_at = updated_at
            record.currency = data["currentTotalPriceSet"]["shopMoney"]["currencyCode"]
            record.shopify_total = total
            record.erp_total = record.quote.total_price.amount if record.quote else None
            record.totals_match = record.is_cancelled or record.erp_total == total
            record.error = ""
            record.synced_at = timezone.now()
            record.save()
            ShopifyInvoicingService.record_refunds(record, data, settled_on_quote=not (locked or invoiced))
            ShopifyFulfillmentService.note_cancellations(record, data.get("fulfillments") or [])

            if record.quote is not None and record.quote.status == QuoteStatus.ACCEPTED and not record.is_cancelled:
                if erp_ships:
                    ShopifyFulfillmentService.create_pending_delivery(record)
                else:
                    ShopifyFulfillmentService.follow_shopify_fulfillments(store, record, data.get("fulfillments") or [])
            ShopifyInvoicingService.apply(record)
            return record

    @classmethod
    def _order_query(cls, store: ShopifyStore) -> str:
        can_read_customers = store.has_scope(AccessScope.READ_CUSTOMERS, AccessScope.WRITE_CUSTOMERS)
        return cls.ORDER_QUERY.replace(cls.CUSTOMER_PLACEHOLDER, cls.CUSTOMER_FIELDS if can_read_customers else "")

    @staticmethod
    def record_failure(store: ShopifyStore, order_id: str, error: Exception) -> None:
        """Keep the reason an order couldn't be imported, for the admin list."""
        ShopifyOrder.objects.update_or_create(
            store=store, order_id=order_id, defaults={"error": str(error)[:2000], "synced_at": timezone.now()}
        )

    @classmethod
    def sync_updated_orders(cls, store: ShopifyStore) -> int:
        """Import every order updated since the last check, in case a webhook was missed."""
        if not store.import_orders or store.orders_synced_at is None:
            return 0
        since = store.orders_synced_at
        nodes = cls.paginate(
            ShopifyClient(store), cls.UPDATED_ORDERS_QUERY, {"query": f"updated_at:>'{since.isoformat()}'"}, ("orders",)
        )
        checked_until = since
        for node in nodes:
            try:
                cls.sync_order(store, node["id"])
            except Exception as exc:  # recorded per order; the rest still import
                cls.record_failure(store, node["id"], exc)
            checked_until = max(checked_until, parse_datetime(node["updatedAt"]))
        store.orders_synced_at = checked_until
        store.save(update_fields=["orders_synced_at", "updated_at"])
        return len(nodes)

    # ── Translation ──────────────────────────────────────────────────────

    @classmethod
    def _external_order(cls, store: ShopifyStore, data: dict, keep_refunded: bool = False) -> ExternalOrder:
        """The order as the quote should hold it; with ``keep_refunded``, refunded goods stay on it."""
        if store.tax_type_id is None:
            raise ShopifyOrderError(_("Choose the store's tax type before importing orders."))
        taxes_included = data["taxesIncluded"]
        refunded = Counter()
        if keep_refunded:
            for refund in data["refunds"]:
                for node in refund["refundLineItems"]["nodes"]:
                    refunded[node["lineItem"]["id"]] += node["quantity"]
        lines = cls._product_lines(store, data["lineItems"]["nodes"], taxes_included, refunded)
        shipping = cls._shipping_line(store, data["shippingLines"]["nodes"], taxes_included)
        if shipping is not None:
            lines.append(shipping)
        return ExternalOrder(
            company=store.company,
            client=cls._client(store, data),
            reference=data["id"],
            name=_("Shopify %(order)s") % {"order": data["name"]},
            date=timezone.localtime(parse_datetime(data["createdAt"])).date(),
            paid=data["displayFinancialStatus"] in OrderFinancialStatus.paid(),
            tax_type=store.tax_type,
            currency=data["currentTotalPriceSet"]["shopMoney"]["currencyCode"],
            lines=lines,
            shipping_address=cls._shipping_address(data),
        )

    @staticmethod
    def _shipping_address(data: dict) -> ExternalAddress | None:
        address = data["shippingAddress"]
        if not address:
            return None
        customer = data.get("customer") or {}
        return ExternalAddress(
            street=" ".join(filter(None, [address.get("address1"), address.get("address2")])),
            city=address.get("city") or "",
            postal_code=address.get("zip") or "",
            province=address.get("province") or "",
            country=address.get("countryCodeV2") or "",
            recipient=address.get("name") or "",
            company=address.get("company") or "",
            phone=address.get("phone") or customer.get("phone") or data["phone"] or "",
        )

    @classmethod
    def _product_lines(
        cls, store: ShopifyStore, nodes: list[dict], taxes_included: bool, refunded: Counter
    ) -> list[ExternalOrderLine]:
        quantity_of = {
            node["id"]: min(node["currentQuantity"] + refunded[node["id"]], node["quantity"]) for node in nodes
        }
        nodes = [node for node in nodes if quantity_of[node["id"]] > 0]  # removed by an order edit or a refund
        variant_ids = [node["variant"]["id"] for node in nodes if node["variant"]]
        items = dict(
            store.variant_links.filter(variant_id__in=variant_ids, status=VariantLinkStatus.MATCHED)
            .values_list("variant_id", "item_id")
        )
        unlinked = [node["name"] for node in nodes if not node["variant"] or node["variant"]["id"] not in items]
        if unlinked:
            raise ShopifyOrderError(
                _("These products are not linked to an ERP item: %(products)s. Fix their SKU, sync the catalog and process the order again.")
                % {"products": ", ".join(unlinked)}
            )
        from inventory.models import InventoryItem

        item_by_pk = InventoryItem.objects.in_bulk(items.values())
        lines = []
        for node in nodes:
            gross = cls.money(node["originalUnitPriceSet"]) * node["quantity"]
            net, rate = cls._net_and_rate(gross, node, taxes_included)
            # An edit can lower the quantity; the order's amounts stay per original line.
            share = Decimal(quantity_of[node["id"]]) / Decimal(node["quantity"])
            lines.append(ExternalOrderLine(
                item=item_by_pk[items[node["variant"]["id"]]],
                quantity=Decimal(quantity_of[node["id"]]),
                net_total=(net * share).quantize(cls.CENT),
                tax_rate=rate,
            ))
        return lines

    @classmethod
    def _shipping_line(cls, store: ShopifyStore, nodes: list[dict], taxes_included: bool) -> ExternalOrderLine | None:
        net, rates = Decimal("0"), set()
        for node in nodes:
            line_net, rate = cls._net_and_rate(cls.money(node["originalPriceSet"]), node, taxes_included)
            net += line_net
            if line_net:
                rates.add(rate)
        if not net:
            return None
        if store.shipping_item_id is None:
            raise ShopifyOrderError(_("The order charges shipping: choose the store's shipping item first."))
        if len(rates) > 1:
            raise ShopifyOrderError(_("Shipping with different tax rates isn't supported yet."))
        return ExternalOrderLine(
            item=store.shipping_item, quantity=Decimal("1"), net_total=net.quantize(cls.CENT), tax_rate=rates.pop()
        )

    @classmethod
    def _net_and_rate(cls, gross: Decimal, node: dict, taxes_included: bool) -> tuple[Decimal, Decimal]:
        discounts = sum((cls.money(d["allocatedAmountSet"]) for d in node["discountAllocations"]), Decimal("0"))
        taxes = sum((cls.money(t["priceSet"]) for t in node["taxLines"]), Decimal("0"))
        rates = {Decimal(str(t["ratePercentage"])) for t in node["taxLines"]}
        if len(rates) > 1:
            raise ShopifyOrderError(_("A line with several taxes isn't supported yet."))
        net = gross - discounts - (taxes if taxes_included else Decimal("0"))
        return net, (rates.pop() if rates else Decimal("0"))

    @staticmethod
    def _client(store: ShopifyStore, data: dict) -> Client:
        if store.client_mode == ClientMode.SINGLE_CLIENT:
            if store.web_client_id is None:
                raise ShopifyOrderError(_("Choose the store's web client before importing orders."))
            return store.web_client

        customer = data.get("customer") or {}
        address = data["billingAddress"] or data["shippingAddress"] or {}
        person = " ".join(filter(None, [customer.get("firstName"), customer.get("lastName")]))
        email = customer.get("email") or data["email"] or ""
        details = ExternalCustomer(
            name=address.get("company") or address.get("name") or person or email or data["name"],
            email=email,
            phone=customer.get("phone") or address.get("phone") or data["phone"] or "",
            street=" ".join(filter(None, [address.get("address1"), address.get("address2")])),
            city=address.get("city") or "",
            postal_code=address.get("zip") or "",
            province=address.get("province") or "",
        )
        return ShopifyOrderService.customer_client(store, customer.get("id"), details)

    @staticmethod
    def customer_client(store: ShopifyStore, customer_id: str | None, details: ExternalCustomer) -> Client:
        """The ERP client of a Shopify customer: the one linked before, else matched or created from ``details``."""
        if customer_id:
            link = ShopifyCustomerLink.objects.filter(store=store, customer_id=customer_id).select_related("client").first()
            if link is not None:
                ExternalOrderService.complete_client(link.client, details)
                return link.client

        client = ExternalOrderService.find_or_create_client(store.company, details)
        if customer_id:
            ShopifyCustomerLink.objects.get_or_create(store=store, customer_id=customer_id, defaults={"client": client})
        return client
