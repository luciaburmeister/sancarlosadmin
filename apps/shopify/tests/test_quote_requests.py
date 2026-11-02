import hashlib
import hmac
import time
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils.translation import gettext
from djmoney.money import Money

from api.fleet.tests.helpers import make_company
from apps.shopify.models import (
    QuoteRequestMode,
    ShopifyApp,
    ShopifyCustomerLink,
    ShopifyStore,
    ShopifyVariantLink,
    VariantLinkStatus,
)
from inventory.choices import ItemType, OnlineSaleMode
from inventory.models import InventoryItem
from invoices.models import AppliedTax, TaxType
from sales.models import Client, Quote, QuoteStatus

SECRET = "app-secret"
TANK_VARIANT = 101
PART_VARIANT = 102


def signed_query(secret=SECRET, timestamp=None, **params):
    """The query string Shopify's app proxy adds, signed as Shopify does."""
    params = {
        "shop": "fistex.myshopify.com",
        "logged_in_customer_id": "",
        "path_prefix": "/apps/erp",
        "timestamp": str(timestamp or int(time.time())),
        **params,
    }
    message = "".join(f"{key}={params[key]}" for key in sorted(params))
    params["signature"] = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


class QuoteRequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        iva = TaxType.objects.create(tax_name="IVA")
        cls.tank = InventoryItem.objects.create(
            company=cls.company, code="T-1", name="Tank 5000 L", item_type=ItemType.SERVICE,
            selling_price=Money("1200.00", "EUR"), online_sale_mode=OnlineSaleMode.QUOTE,
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(InventoryItem), object_id=cls.tank.pk,
            tax_type=iva, selected_percentage=21,
        )
        cls.part = InventoryItem.objects.create(
            company=cls.company, code="P-1", name="Valve", item_type=ItemType.SERVICE, selling_price=Money("20.00", "EUR"),
        )
        InventoryItem.objects.filter(pk__in=[cls.tank.pk, cls.part.pk]).update(sell_online=True)
        app = ShopifyApp(name="App", client_id="client-1")
        app.set_client_secret(SECRET)
        app.save()
        cls.app = app

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, app=self.app, name="Shop", shop_domain="fistex.myshopify.com",
            quote_requests=QuoteRequestMode.ALONGSIDE,
        )
        for variant, item in ((TANK_VARIANT, self.tank), (PART_VARIANT, self.part)):
            ShopifyVariantLink.objects.create(
                store=self.store, product_id=f"p{variant}", variant_id=f"gid://shopify/ProductVariant/{variant}",
                sku=item.code, item=item, status=VariantLinkStatus.MATCHED, synced_at="2026-10-21T08:00Z",
            )

    def request(self, query=None, **form):
        data = {"variant_id": TANK_VARIANT, "quantity": 2, "name": "Ana García", "email": "ana@example.com",
                "phone": "+34600111222", "notes": "Delivery to Valencia, ground floor", **form}
        return self.client.post(f"{reverse('shopify_quote_request')}?{query or signed_query()}", data)

    def test_a_request_becomes_a_draft_quote_priced_by_the_erp(self):
        response = self.request()

        self.assertEqual(response.status_code, 200, response.content)
        quote = Quote.objects.get()
        self.assertEqual((quote.status, quote.company), (QuoteStatus.DRAFT, self.company))
        self.assertEqual(quote.project_name, gettext("Web quote request: %(product)s") % {"product": "Tank 5000 L"})
        self.assertEqual(quote.observations, "Delivery to Valencia, ground floor")
        line = quote.inventory_items.get()
        self.assertEqual((line.inventory_item, line.quantity, line.line_total.amount), (self.tank, Decimal("2"), Decimal("2400.00")))
        self.assertEqual(quote.total_price.amount, Decimal("2904.00"))  # the item's own 21 % tax
        self.assertEqual((quote.client.name, quote.client.email, quote.client.phone), ("Ana García", "ana@example.com", "+34600111222"))
        self.assertIsNone(quote.project_file)  # only an accepted quote gets an expediente

    def test_requests_must_be_signed_by_shopify_and_recent(self):
        self.assertEqual(self.request(signed_query(secret="wrong")).status_code, 401)
        self.assertEqual(self.request(signed_query(timestamp=int(time.time()) - 3600)).status_code, 401)
        tampered = signed_query().replace("fistex.myshopify.com", "other.myshopify.com")
        self.assertEqual(self.request(tampered).status_code, 401)
        self.assertFalse(Quote.objects.exists())

    def test_stores_without_quote_requests_and_products_sold_only_by_checkout(self):
        self.assertEqual(self.request(variant_id=PART_VARIANT).status_code, 400)  # its item is sold as "Buy"

        self.store.quote_requests = QuoteRequestMode.INSTEAD  # then every product can be quoted
        self.store.save()
        self.assertEqual(self.request(variant_id=PART_VARIANT).status_code, 200)

        self.store.quote_requests = QuoteRequestMode.OFF
        self.store.save()
        self.assertEqual(self.request().status_code, 404)
        self.assertEqual(Quote.objects.count(), 1)

    def test_bots_and_invalid_forms_create_nothing(self):
        self.assertEqual(self.request(website="http://spam.example").status_code, 200)
        response = self.request(email="not-an-email", quantity=0)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()["errors"]), {"email", "quantity"})
        self.assertFalse(Quote.objects.exists())

    def test_logged_in_customers_and_known_emails_reuse_their_client(self):
        existing = Client.objects.create(company=self.company, name="García SL", email="ana@example.com")
        self.request(signed_query(logged_in_customer_id="77"))
        self.assertEqual(Quote.objects.get().client, existing)
        self.assertEqual(ShopifyCustomerLink.objects.get().customer_id, "gid://shopify/Customer/77")

        self.request(signed_query(logged_in_customer_id="77"), email="other@example.com")
        self.assertEqual(Quote.objects.latest("pk").client, existing)  # Shopify vouches for who is logged in
        self.assertEqual(Client.objects.filter(company=self.company).count(), 1)
