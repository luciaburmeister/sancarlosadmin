from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from api.fleet.tests.helpers import make_company, make_user
from apps.shopify.admin import ShopifyStoreAdmin
from apps.shopify.models import ShopifyApp, ShopifyStore
from company.models import CompanyUser


class ShopifyStoreModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company_a = make_company(name="Company A")
        cls.company_b = make_company(name="Company B")
        cls.app = ShopifyApp.objects.create(name="Pilot app", client_id="client-123")

    def test_pasted_url_is_normalised_to_myshopify_domain(self):
        store = ShopifyStore(company=self.company_a, app=self.app, name="Shop", shop_domain=" https://My-Store.myshopify.com/admin ")
        store.full_clean()
        self.assertEqual(store.shop_domain, "my-store.myshopify.com")

    def test_custom_domain_is_rejected(self):
        store = ShopifyStore(company=self.company_a, app=self.app, name="Shop", shop_domain="shop.example.com")
        with self.assertRaises(ValidationError) as ctx:
            store.full_clean()
        self.assertEqual(list(ctx.exception.message_dict), ["shop_domain"])

    def test_a_store_belongs_to_one_company_only(self):
        ShopifyStore.objects.create(company=self.company_a, app=self.app, name="A", shop_domain="shared.myshopify.com")
        duplicate = ShopifyStore(company=self.company_b, app=self.app, name="B", shop_domain="shared.myshopify.com")
        with self.assertRaises(ValidationError) as ctx:
            duplicate.full_clean()
        self.assertEqual(list(ctx.exception.message_dict), ["shop_domain"])

    def test_tokens_are_stored_encrypted(self):
        store = ShopifyStore.objects.create(company=self.company_a, name="A", shop_domain="a.myshopify.com")
        self.assertFalse(store.is_connected)
        expires = timezone.now() + timedelta(hours=1)
        store.set_tokens(
            access_token="shpat_access",
            access_token_expires_at=expires,
            refresh_token="shprt_refresh",
            refresh_token_expires_at=expires + timedelta(days=90),
        )
        store.save()

        store.refresh_from_db()
        self.assertTrue(store.is_connected)
        self.assertNotIn("shpat_access", store.access_token_encrypted)
        self.assertNotIn("shprt_refresh", store.refresh_token_encrypted)
        self.assertEqual(store.get_access_token(), "shpat_access")
        self.assertEqual(store.get_refresh_token(), "shprt_refresh")

    def test_missing_access_token_is_refused(self):
        store = ShopifyStore(company=self.company_a, name="A", shop_domain="a.myshopify.com")
        with self.assertRaises(ValidationError):
            store.set_tokens(access_token="", access_token_expires_at=None, refresh_token="", refresh_token_expires_at=None)


class ShopifyStoreAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company_a = make_company(name="Company A")
        cls.company_b = make_company(name="Company B")
        cls.user = make_user(username="staff_a")
        CompanyUser.objects.create(user=cls.user, company=cls.company_a, is_active=True)
        cls.store_a = ShopifyStore.objects.create(company=cls.company_a, name="A", shop_domain="a.myshopify.com")
        cls.store_b = ShopifyStore.objects.create(company=cls.company_b, name="B", shop_domain="b.myshopify.com")

    def setUp(self):
        self.admin = ShopifyStoreAdmin(ShopifyStore, admin_site=None)
        self.request = RequestFactory().get("/")
        self.request.user = self.user

    def test_only_own_company_stores_are_listed(self):
        self.assertEqual(list(self.admin.get_queryset(self.request)), [self.store_a])

    def test_store_cannot_be_assigned_to_another_company(self):
        field = ShopifyStore._meta.get_field("company")
        formfield = self.admin.formfield_for_foreignkey(field, self.request)
        self.assertIn(self.company_a, formfield.queryset)
        self.assertNotIn(self.company_b, formfield.queryset)

    def test_connected_store_keeps_its_domain_and_company(self):
        self.store_a.access_token_encrypted = "encrypted"
        readonly = self.admin.get_readonly_fields(self.request, self.store_a)
        self.assertIn("shop_domain", readonly)
        self.assertIn("company", readonly)
