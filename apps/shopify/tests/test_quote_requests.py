import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext
from djmoney.money import Money

from api.fleet.tests.helpers import make_company
from apps.shopify.models import (
    MadeToMeasureMode,
    QuoteRequestMode,
    ShopifyApp,
    ShopifyCustomerLink,
    ShopifyQuote,
    ShopifyStore,
    ShopifyVariantLink,
    VariantLinkStatus,
)
from inventory.choices import ItemType, OnlineSaleMode
from inventory.models import InventoryItem
from invoices.models import AppliedTax, TaxType
from configurator.models import ConfigurationPreset, ParameterType, ProductParameter, ProductTemplate
from sales.models import Client, Quote, QuoteStatus

SECRET = "app-secret"
TANK_VARIANT = 101
PART_VARIANT = 102
MADE_TO_MEASURE_VARIANT = 103


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
        # Made to measure: a tank preset with the coating fixed and the volume left to the customer.
        template = ProductTemplate.objects.create(company=cls.company, name="CA/TA")
        ProductParameter.objects.create(product=template, name="VOLUMEN", label="Volumen (L)", type=ParameterType.SELECT,
                                        options=[160, 200, 300], order=0)
        ProductParameter.objects.create(product=template, name="RECUBRIMIENTO", label="Recubrimiento",
                                        type=ParameterType.SELECT, options=["F.Poliester", "F.Skay"], order=1)
        cls.preset = ConfigurationPreset.objects.create(
            company=cls.company, product=template, name="CA/TA a medida", parameter_values={"RECUBRIMIENTO": "F.Skay"},
        )
        cls.custom_tank = InventoryItem.objects.create(
            company=cls.company, code="CATA", name="CA/TA a medida", item_type=ItemType.OWN_PRODUCTION,
            product_content_type=ContentType.objects.get_for_model(ConfigurationPreset), product_object_id=cls.preset.pk,
        )
        InventoryItem.objects.filter(pk__in=[cls.tank.pk, cls.part.pk, cls.custom_tank.pk]).update(sell_online=True)
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
        for variant, item in ((TANK_VARIANT, self.tank), (PART_VARIANT, self.part), (MADE_TO_MEASURE_VARIANT, self.custom_tank)):
            ShopifyVariantLink.objects.create(
                store=self.store, product_id=f"p{variant}", variant_id=f"gid://shopify/ProductVariant/{variant}",
                sku=item.code, item=item, status=VariantLinkStatus.MATCHED, synced_at="2026-10-21T08:00Z",
            )

    def request(self, query=None, lines=None, **form):
        data = {"lines": json.dumps(lines or [{"variant_id": TANK_VARIANT, "quantity": 2}]), "name": "Ana García",
                "email": "ana@example.com", "phone": "+34600111222", "notes": "Delivery to Valencia, ground floor", **form}
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
        part = [{"variant_id": PART_VARIANT, "quantity": 1}]
        self.assertEqual(self.request(lines=part).status_code, 400)  # its item is sold as "Buy"

        self.store.quote_requests = QuoteRequestMode.INSTEAD  # then every product can be quoted
        self.store.save()
        self.assertEqual(self.request(lines=part).status_code, 200)

        self.store.quote_requests = QuoteRequestMode.OFF
        self.store.save()
        self.assertEqual(self.request().status_code, 404)
        self.assertEqual(Quote.objects.count(), 1)

    def test_bots_and_invalid_forms_create_nothing(self):
        self.assertEqual(self.request(website="http://spam.example").status_code, 200)
        response = self.request(email="not-an-email", lines=[{"variant_id": TANK_VARIANT, "quantity": 0}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()["errors"]), {"email", "lines"})
        self.assertFalse(Quote.objects.exists())

    def test_known_emails_reuse_their_client_without_linking_the_customer_to_it(self):
        existing = Client.objects.create(company=self.company, name="García SL", email="ana@example.com")
        self.request(signed_query(logged_in_customer_id="77"))

        self.assertEqual(Quote.objects.get().client, existing)
        # Anyone signed in can type a client's email: that never links them to the client.
        self.assertFalse(ShopifyCustomerLink.objects.exists())
        # Recorded as requested on this store, for the customer's quotes area.
        record = ShopifyQuote.objects.get()
        self.assertEqual((record.store, record.quote, record.customer_id), (self.store, Quote.objects.get(), "gid://shopify/Customer/77"))

    def test_a_linked_customer_keeps_their_client_whatever_email_they_type(self):
        existing = Client.objects.create(company=self.company, name="García SL", email="ana@example.com")
        ShopifyCustomerLink.objects.create(store=self.store, customer_id="gid://shopify/Customer/77", client=existing)

        self.request(signed_query(logged_in_customer_id="77"), email="other@example.com")

        self.assertEqual(Quote.objects.get().client, existing)  # Shopify vouches for who is signed in
        self.assertEqual(Client.objects.filter(company=self.company).count(), 1)

    def test_the_quote_is_titled_in_the_erps_language_and_the_customer_answered_in_theirs(self):
        query = signed_query()
        url = f"{reverse('shopify_quote_request')}?{query}"
        lines = json.dumps([{"variant_id": TANK_VARIANT, "quantity": 1}])
        data = {"lines": lines, "name": "Ana", "email": "ana@example.com", "phone": "", "notes": ""}

        self.client.post(url, data, HTTP_ACCEPT_LANGUAGE="en")
        refused = self.client.post(
            f"{reverse('shopify_quote_request')}?{signed_query()}",
            {**data, "lines": json.dumps([{"variant_id": PART_VARIANT, "quantity": 1}])},
            HTTP_ACCEPT_LANGUAGE="en",
        )

        with translation.override("es"):
            title = translation.gettext("Web quote request: %(product)s") % {"product": "Tank 5000 L"}
        self.assertEqual(Quote.objects.get().project_name, title)
        self.assertEqual(refused.json()["error"], "This product can't be quoted.")

    def test_requirements_per_product_reach_the_quotes_observations(self):
        lines = [
            {"variant_id": TANK_VARIANT, "quantity": 1, "notes": "With 4 flanged 4\" connections"},
            {"variant_id": TANK_VARIANT, "quantity": 2},
        ]
        response = self.request(lines=lines, notes="Site access only in the mornings")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Quote.objects.get().observations, (
            "Site access only in the mornings\n\n• Tank 5000 L: With 4 flanged 4\" connections"
        ))

        too_long = [{"variant_id": TANK_VARIANT, "quantity": 1, "notes": "x" * 501}]
        self.assertEqual(self.request(lines=too_long).status_code, 400)

    def test_several_products_become_one_quote(self):
        self.store.quote_requests = QuoteRequestMode.INSTEAD
        self.store.save()

        response = self.request(lines=[{"variant_id": TANK_VARIANT, "quantity": 2}, {"variant_id": PART_VARIANT, "quantity": 5}])

        self.assertEqual(response.status_code, 200, response.content)
        quote = Quote.objects.get()
        self.assertEqual(
            list(quote.inventory_items.order_by("order").values_list("inventory_item__code", "quantity")),
            [("T-1", Decimal("2")), ("P-1", Decimal("5"))],
        )
        self.assertEqual(
            quote.project_name,
            gettext("Web quote request: %(product)s and %(count)s more") % {"product": "Tank 5000 L", "count": 1},
        )

    def test_a_request_with_a_product_that_cant_be_quoted_creates_nothing(self):
        response = self.request(lines=[{"variant_id": TANK_VARIANT, "quantity": 2}, {"variant_id": PART_VARIANT, "quantity": 1}])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Quote.objects.exists())
        self.assertFalse(Client.objects.filter(company=self.company).exists())


class MadeToMeasureRequestTests(QuoteRequestTests):
    """Made-to-measure lines; the plain-request tests above also run with made to measure on."""

    def setUp(self):
        super().setUp()
        self.store.made_to_measure = MadeToMeasureMode.QUOTE
        self.store.save()

    def test_stores_without_quote_requests_and_products_sold_only_by_checkout(self):
        # Made to measure keeps the endpoint open; plain products still follow the quote request setting.
        self.store.quote_requests = QuoteRequestMode.OFF
        self.store.save()

        self.assertEqual(self.request().status_code, 400)
        self.assertEqual(self.request(lines=self.custom(VOLUMEN="300")).status_code, 200)
        self.assertEqual(Quote.objects.get().configurations.count(), 1)

    def custom(self, **parameters):
        return [{"variant_id": MADE_TO_MEASURE_VARIANT, "quantity": 1, "parameters": parameters}]

    def test_the_customers_values_become_a_configuration_on_the_quote(self):
        response = self.request(lines=self.custom(VOLUMEN="300"))

        self.assertEqual(response.status_code, 200, response.content)
        quote = Quote.objects.get()
        configuration = quote.configurations.get()
        self.assertEqual((configuration.product, configuration.client), (self.preset.product, quote.client))
        self.assertEqual(
            dict(configuration.param_values.values_list("parameter__name", "value")),
            {"VOLUMEN": "300", "RECUBRIMIENTO": "F.Skay"},  # the preset's own value is kept
        )
        self.assertFalse(quote.inventory_items.exists())

    def test_only_the_presets_open_parameters_and_their_options_are_accepted(self):
        for case, parameters in {
            "option not offered": {"VOLUMEN": "350"},
            "fixed by the preset": {"VOLUMEN": "300", "RECUBRIMIENTO": "F.Poliester"},
            "missing": {"OTHER": "1"},
        }.items():
            with self.subTest(case):
                response = self.request(lines=self.custom(**parameters))

                self.assertEqual(response.status_code, 400)
        self.assertIn("Volumen (L)", self.request(lines=self.custom(VOLUMEN="350")).json()["error"])
        self.assertFalse(Quote.objects.exists())
        self.assertFalse(Client.objects.filter(company=self.company).exists())

    def test_made_to_measure_items_need_values_and_only_they_take_them(self):
        no_values = [{"variant_id": MADE_TO_MEASURE_VARIANT, "quantity": 1}]
        values_on_a_plain_item = [{"variant_id": TANK_VARIANT, "quantity": 1, "parameters": {"VOLUMEN": "300"}}]

        self.assertEqual(self.request(lines=no_values).status_code, 400)
        self.assertEqual(self.request(lines=values_on_a_plain_item).status_code, 400)
        self.assertFalse(Quote.objects.exists())

    def test_stores_without_made_to_measure_refuse_it(self):
        self.store.made_to_measure = MadeToMeasureMode.OFF
        self.store.save()

        self.assertEqual(self.request(lines=self.custom(VOLUMEN="300")).status_code, 400)
        self.assertFalse(Quote.objects.exists())

    def test_a_preset_of_another_company_is_not_made_to_measure(self):
        other = make_company(name="Other")
        ConfigurationPreset.objects.filter(pk=self.preset.pk).update(company=other)

        self.assertEqual(self.request(lines=self.custom(VOLUMEN="300")).status_code, 400)
        self.assertFalse(Quote.objects.exists())
