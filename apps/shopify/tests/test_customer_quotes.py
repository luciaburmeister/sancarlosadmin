from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djmoney.money import Money

from api.fleet.tests.helpers import make_company
from apps.shopify.models import CustomerQuotesMode, ShopifyApp, ShopifyCustomerLink, ShopifyQuote, ShopifyStore
from apps.shopify.tests.test_quote_requests import SECRET, signed_query
from management.models import ProjectFile
from sales.models import Client, Quote, QuoteStatus

CUSTOMER = "77"


class CustomerQuotesTests(TestCase):
    """A store's quotes area: a signed-in customer's quotes, their PDF, and accepting one."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        app = ShopifyApp(name="App", client_id="client-1")
        app.set_client_secret(SECRET)
        app.save()
        cls.app = app
        cls.client_ = Client.objects.create(company=cls.company, name="Instalaciones García")
        other_client = Client.objects.create(company=cls.company, name="Otro cliente")

        def quote(name, status, client=None, **fields):
            return Quote.objects.create(
                company=cls.company, client=client or cls.client_, project_name=name, status=status,
                total_price=Money("1210.00", "EUR"), **fields,
            )

        cls.requested = quote("Requested on the web", QuoteStatus.DRAFT)
        cls.sent = quote("Sent by staff", QuoteStatus.SENT, expires_at=timezone.localdate() + timedelta(days=30))
        cls.expired = quote("Expired", QuoteStatus.SENT, expires_at=timezone.localdate() - timedelta(days=1))
        cls.accepted = quote("Accepted", QuoteStatus.ACCEPTED)
        cls.staff_draft = quote("Staff still working on it", QuoteStatus.DRAFT)
        cls.rejected = quote("Rejected", QuoteStatus.REJECTED)
        cls.other_clients = quote("Another client's", QuoteStatus.SENT, client=other_client)

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, app=self.app, name="Shop", shop_domain="fistex.myshopify.com",
            customer_quotes=CustomerQuotesMode.ALL,
        )
        ShopifyCustomerLink.objects.create(
            store=self.store, customer_id=f"gid://shopify/Customer/{CUSTOMER}", client=self.client_, web_store_access=True,
        )
        for quote in (self.requested, self.sent):
            ShopifyQuote.objects.create(store=self.store, quote=quote, customer_id=f"gid://shopify/Customer/{CUSTOMER}")

    def get_list(self, customer=CUSTOMER):
        return self.client.get(f"{reverse('shopify_customer_quotes')}?{signed_query(logged_in_customer_id=customer)}")

    def accept(self, quote, customer=CUSTOMER):
        url = reverse("shopify_customer_quote_accept", args=[quote.pk])
        return self.client.post(f"{url}?{signed_query(logged_in_customer_id=customer)}")

    def names(self, response):
        return {quote["name"] for quote in response.json()["quotes"]}

    def test_all_quotes_sent_to_the_client_and_its_web_requests_being_prepared(self):
        response = self.get_list()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.names(response), {"Requested on the web", "Sent by staff", "Expired", "Accepted"})
        by_name = {quote["name"]: quote for quote in response.json()["quotes"]}
        self.assertEqual(by_name["Requested on the web"]["state"], "requested")
        self.assertIsNone(by_name["Requested on the web"]["total"])  # no price before staff send it
        self.assertEqual((by_name["Sent by staff"]["state"], by_name["Sent by staff"]["total"]), ("sent", "1210.00"))
        self.assertTrue(by_name["Sent by staff"]["can_accept"])
        self.assertEqual((by_name["Expired"]["state"], by_name["Expired"]["can_accept"]), ("expired", False))

    def test_stores_can_show_only_the_quotes_requested_on_the_website(self):
        self.store.customer_quotes = CustomerQuotesMode.REQUESTED
        self.store.save()

        self.assertEqual(self.names(self.get_list()), {"Requested on the web", "Sent by staff"})

    def test_only_signed_in_customers_with_web_store_access_and_stores_that_show_quotes(self):
        self.assertEqual(self.get_list(customer="").status_code, 401)
        self.assertEqual(self.get_list(customer="999").status_code, 403)  # no link to a client

        ShopifyCustomerLink.objects.update(web_store_access=False)  # linked, but staff never gave access
        self.assertEqual(self.get_list().status_code, 403)

        ShopifyCustomerLink.objects.update(web_store_access=True)
        self.store.customer_quotes = CustomerQuotesMode.OFF
        self.store.save()
        self.assertEqual(self.get_list().status_code, 404)

    def test_calls_must_be_signed_by_shopify(self):
        response = self.client.get(f"{reverse('shopify_customer_quotes')}?{signed_query(secret='wrong', logged_in_customer_id=CUSTOMER)}")
        self.assertEqual(response.status_code, 401)

    def test_accepting_a_sent_quote_files_it_under_a_new_expediente(self):
        response = self.accept(self.sent)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["quote"]["state"], "accepted")
        self.sent.refresh_from_db()
        self.assertEqual(self.sent.status, QuoteStatus.ACCEPTED)
        self.assertIsNotNone(self.sent.date_accepted)
        self.assertEqual(
            (self.sent.project_file.name, self.sent.project_file.client), ("Sent by staff", self.client_)
        )

        self.assertEqual(self.accept(self.sent).status_code, 400)  # only once
        self.assertEqual(ProjectFile.objects.count(), 1)

    def test_drafts_expired_and_other_clients_quotes_cannot_be_accepted(self):
        self.assertEqual(self.accept(self.requested).status_code, 400)
        self.assertEqual(self.accept(self.expired).status_code, 400)
        self.assertEqual(self.accept(self.other_clients).status_code, 404)
        self.assertEqual(self.accept(self.rejected).status_code, 404)
        self.assertFalse(ProjectFile.objects.exists())
        self.assertEqual(Quote.objects.get(pk=self.other_clients.pk).status, QuoteStatus.SENT)

    def test_the_pdf_of_a_priced_quote_of_the_client(self):
        def pdf(quote):
            url = reverse("shopify_customer_quote_pdf", args=[quote.pk])
            return self.client.get(f"{url}?{signed_query(logged_in_customer_id=CUSTOMER)}")

        with mock.patch("apps.shopify.views.render_quote_pdf", return_value=b"%PDF-1.7") as render:
            response = pdf(self.sent)
            self.assertEqual((response.status_code, response["Content-Type"]), (200, "application/pdf"))
            self.assertEqual(response.content, b"%PDF-1.7")
            self.assertEqual(pdf(self.requested).status_code, 404)  # not priced yet
            self.assertEqual(pdf(self.other_clients).status_code, 404)
        render.assert_called_once_with(self.sent.pk)
