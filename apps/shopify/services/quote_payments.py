"""
Paying an accepted quote online, through Shopify's checkout.

A cart can't charge a price agreed in a quote, so the ERP creates a Shopify
draft order for it and sends the customer to its checkout. The draft has one
custom line for the quote's total with its taxes, neither taxed again nor
shipped by Shopify and closed to discount codes, so the customer pays exactly
the quote. It names the quote (``QUOTE_ATTRIBUTE``); once paid, the order
import checks with Shopify that the order is that draft's and files it under
the quote instead of building a new one (``ShopifyOrderService``). Invoicing
and recording the payment then follow the store's settings, as for any order.
"""

from __future__ import annotations

from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.shopify.client import ShopifyClient
from apps.shopify.enums import AccessScope, DraftOrderStatus
from apps.shopify.exceptions import ShopifyBusy
from apps.shopify.models import ShopifyQuotePayment, ShopifyStore
from apps.shopify.services.base import ShopifyService
from sales.models import Quote, QuoteStatus


class QuotePaymentError(Exception):
    """The quote can't be paid online; the message says why."""


class ShopifyQuotePaymentService(ShopifyService):
    QUOTE_ATTRIBUTE = "polipus_quote"
    DRAFT_FIELDS = "id status invoiceUrl totalPriceSet { shopMoney { amount currencyCode } } order { id }"
    DRAFT_QUERY = f"query ($id: ID!) {{ draftOrder(id: $id) {{ {DRAFT_FIELDS} }} }}"
    DRAFT_CREATE = f"""
    mutation ($input: DraftOrderInput!) {{
      draftOrderCreate(input: $input) {{ draftOrder {{ {DRAFT_FIELDS} }} userErrors {{ field message }} }}
    }}
    """
    DRAFT_DELETE = """
    mutation ($input: DraftOrderDeleteInput!) { draftOrderDelete(input: $input) { deletedId userErrors { field message } } }
    """
    LOCK = "shopify:quote-payment:{quote_id}"
    LOCK_SECONDS = 60

    @staticmethod
    def takes_payments(store: ShopifyStore) -> bool:
        return store.pay_quotes_online and store.has_scope(AccessScope.WRITE_DRAFT_ORDERS)

    @classmethod
    def can_pay(cls, store: ShopifyStore, quote: Quote) -> bool:
        """An accepted quote with something to pay, in a store that takes payments.

        Not one with a Shopify order already: a web order's quote was paid
        at checkout, and a quote paid here gets its payment's order. Not one
        sharing its expediente or with invoices in it either (``_settles_alone``).
        """
        return (
            cls.takes_payments(store)
            and quote.status == QuoteStatus.ACCEPTED
            and quote.total_price is not None
            and quote.total_price.amount > 0
            and getattr(quote, "shopify_order", None) is None
            and cls._settles_alone(quote)
        )

    @staticmethod
    def _settles_alone(quote: Quote) -> bool:
        """The quote is all its expediente holds and nothing there is invoiced yet.

        The payment's order is invoiced, and its card payment recorded, for the
        whole expediente, so it may only stand for this one quote. Reads the
        quote's ``QuoteQuerySet.with_project_file_activity`` annotations, which
        the quotes area loads with its list.
        """
        return quote.project_file_id is not None and not (
            quote.project_file_has_other_quotes or quote.project_file_has_invoices
        )

    @classmethod
    def checkout_url(cls, store: ShopifyStore, quote: Quote, customer_id: str) -> str:
        """Where the customer pays the quote: its draft order's checkout, made or brought up to date.

        Not inside a database transaction: it calls Shopify. A lock per
        quote keeps two clicks from making two drafts.
        """
        if not cls.can_pay(store, quote):
            raise QuotePaymentError(_("This quote can't be paid online."))
        busy = ShopifyBusy(_("The payment is being prepared. Try again in a moment."))
        with cls.store_lock(cls.LOCK.format(quote_id=quote.pk), cls.LOCK_SECONDS, busy):
            client = ShopifyClient(store)
            payment = ShopifyQuotePayment.objects.filter(quote=quote).first()
            amount, currency = quote.total_price.amount, str(quote.total_price.currency)
            if payment is not None:
                draft = client.graphql(cls.DRAFT_QUERY, {"id": payment.draft_order_id})["draftOrder"]
                if draft is not None and draft["order"] is not None:
                    raise QuotePaymentError(_("This quote is already paid."))
                still_valid = (payment.amount, payment.currency, payment.customer_id) == (amount, currency, customer_id)
                if draft is not None and draft["status"] != DraftOrderStatus.COMPLETED and still_valid:
                    return payment.checkout_url
                if draft is not None:
                    cls._delete(client, payment.draft_order_id)  # the quote or customer changed since
            draft = cls._create(client, quote, customer_id)
            ShopifyQuotePayment.objects.update_or_create(quote=quote, defaults={
                "store": store,
                "draft_order_id": draft["id"],
                "checkout_url": draft["invoiceUrl"],
                "amount": amount,
                "currency": currency,
                "customer_id": customer_id,
            })
            return draft["invoiceUrl"]

    @classmethod
    def _create(cls, client: ShopifyClient, quote: Quote, customer_id: str) -> dict:
        title = cls.erp_text(gettext_noop("Quote no. %(number)s"), number=quote.pk)
        if quote.project_name:
            title = f"{title}: {quote.project_name}"
        attribute = [{"key": cls.QUOTE_ATTRIBUTE, "value": str(quote.pk)}]
        data = client.graphql(cls.DRAFT_CREATE, {"input": {
            "purchasingEntity": {"customerId": customer_id},
            "lineItems": [{
                "title": title[:255],
                "quantity": 1,
                "originalUnitPriceWithCurrency": {
                    "amount": str(quote.total_price.amount), "currencyCode": str(quote.total_price.currency),
                },
                # The quote's total already holds its taxes, and the ERP delivers it.
                "taxable": False,
                "requiresShipping": False,
                "customAttributes": attribute,
            }],
            "customAttributes": attribute,
            "allowDiscountCodesInCheckout": False,
            "acceptAutomaticDiscounts": False,
            "tags": [cls.QUOTE_ATTRIBUTE.replace("_", "-")],
        }})["draftOrderCreate"]
        cls.raise_user_errors(data["userErrors"], _("Shopify refused to prepare the payment: %(detail)s"))
        draft = data["draftOrder"]
        if cls.money(draft["totalPriceSet"]) != quote.total_price.amount:
            # Never send the customer to pay anything but the quote.
            cls._delete(client, draft["id"])
            raise QuotePaymentError(_("The payment doesn't match the quote's total. We'll be in touch."))
        return draft

    @classmethod
    def _delete(cls, client: ShopifyClient, draft_order_id: str) -> None:
        data = client.graphql(cls.DRAFT_DELETE, {"input": {"id": draft_order_id}})["draftOrderDelete"]
        cls.raise_user_errors(data["userErrors"], _("Shopify refused to prepare the payment: %(detail)s"))

    @classmethod
    def payment_of_order(cls, store: ShopifyStore, client: ShopifyClient, order: dict) -> ShopifyQuotePayment | None:
        """The quote payment an order completes, checked with Shopify; ``None`` for any other order.

        The quote the order names is only a hint: anyone can add attributes to
        a cart. The order counts as the payment only if it is the order
        Shopify made from that quote's draft.
        """
        quote_id = next(
            (
                attribute["value"]
                for attribute in [*(order.get("customAttributes") or []), *cls._line_attributes(order)]
                if attribute["key"] == cls.QUOTE_ATTRIBUTE
            ),
            None,
        )
        if not quote_id or not quote_id.isdigit():
            return None
        payment = (
            ShopifyQuotePayment.objects.filter(store=store, quote_id=int(quote_id)).select_related("quote").first()
        )
        if payment is None:
            return None
        draft = client.graphql(cls.DRAFT_QUERY, {"id": payment.draft_order_id})["draftOrder"]
        if draft is None or draft["order"] is None or draft["order"]["id"] != order["id"]:
            return None
        return payment

    @staticmethod
    def _line_attributes(order: dict) -> list[dict]:
        return [
            attribute
            for line in order.get("lineItems", {}).get("nodes", [])
            for attribute in line.get("customAttributes") or []
        ]
