import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext

from apps.shopify.client import ShopifyClient
from apps.shopify.exceptions import ShopifyAPIError, ShopifyAuthError, ShopifyBusy
from apps.shopify.models import ShopifyCustomerLink, ShopifyDiscountProfile, ShopifyStore
from apps.shopify.services.discounts import ClientAccessError, ShopifyDiscountService
from apps.shopify.services.push_scheduler import ShopifyPushScheduler
from apps.shopify.tasks import sync_client_discounts
from api.fleet.tests.helpers import make_user
from company.models import CompanyDefaultTax, CompanyUser
from invoices.models import TaxType
from sales.models import DiscountLevel, DiscountMode
from sales.tests.factories import make_client, make_company, make_discount_policy

SCOPES = "read_customers,write_customers,write_discounts"


class FakeShopify:
    """Keeps the store's collections, code discounts and customer metafields, and answers the discount calls."""

    def __init__(self, taxes_included=False, fail_on_create=None):
        self.taxes_included = taxes_included
        self.fail_on_create = fail_on_create  # the nth discount creation fails
        self.fail_metafields = False
        self.collections = set()
        self.discounts = {}  # id -> {"input": ..., "customers": set()}
        self.metafields = {}  # (customer id, key) -> value
        self.customers = {}  # id -> email
        self.mutations = []
        self.created = 0

    def __call__(self, query, variables=None):
        variables = variables or {}
        if "taxesIncluded" in query:
            return {"shop": {"taxesIncluded": self.taxes_included}}
        if "collectionCreate" in query:
            self.mutations.append("collectionCreate")
            gid = f"gid://shopify/Collection/{len(self.collections) + 1}"
            self.collections.add(gid)
            return {"collectionCreate": {"collection": {"id": gid}, "userErrors": []}}
        if "collection(id" in query:
            return {"collection": {"id": variables["id"]} if variables["id"] in self.collections else None}
        if "discountCodeBasicCreate" in query:
            self.mutations.append("discountCodeBasicCreate")
            self.created += 1
            if self.created == self.fail_on_create:
                return {"discountCodeBasicCreate": {"codeDiscountNode": None, "userErrors": [{"field": None, "message": "boom"}]}}
            gid = f"gid://shopify/DiscountCodeNode/{self.created}"
            discount = variables["discount"]
            self.discounts[gid] = {"input": discount, "customers": set(discount["context"]["customers"]["add"])}
            return {"discountCodeBasicCreate": {"codeDiscountNode": {"id": gid}, "userErrors": []}}
        if "discountCodeBasicUpdate" in query:
            self.mutations.append("discountCodeBasicUpdate")
            customers = variables["discount"]["context"]["customers"]
            discount = self.discounts[variables["id"]]
            discount["customers"] |= set(customers.get("add", []))
            discount["customers"] -= set(customers.get("remove", []))
            return {"discountCodeBasicUpdate": {"codeDiscountNode": {"id": variables["id"]}, "userErrors": []}}
        if "discountCodeDelete" in query:
            self.mutations.append("discountCodeDelete")
            if self.discounts.pop(variables["id"], None) is None:
                return {"discountCodeDelete": {"deletedCodeDiscountId": None, "userErrors": [{"field": None, "message": "missing"}]}}
            return {"discountCodeDelete": {"deletedCodeDiscountId": variables["id"], "userErrors": []}}
        if "codeDiscountNode(id" in query:
            return {"codeDiscountNode": {"id": variables["id"]} if variables["id"] in self.discounts else None}
        if "metafieldsSet" in query:
            self.mutations.append("metafieldsSet")
            if self.fail_metafields:
                return {"metafieldsSet": {"userErrors": [{"field": None, "message": "down"}]}}
            for metafield in variables["metafields"]:
                self.metafields[(metafield["ownerId"], metafield["key"])] = metafield["value"]
            return {"metafieldsSet": {"userErrors": []}}
        if "metafieldsDelete" in query:
            self.mutations.append("metafieldsDelete")
            for metafield in variables["metafields"]:
                self.metafields.pop((metafield["ownerId"], metafield["key"]), None)
            return {"metafieldsDelete": {"userErrors": []}}
        if "customerCreate" in query:
            self.mutations.append("customerCreate")
            gid = f"gid://shopify/Customer/{100 + len(self.customers)}"
            self.customers[gid] = variables["input"]["email"]
            return {"customerCreate": {"customer": {"id": gid}, "userErrors": []}}
        if "customers(" in query:
            wanted = variables["query"].removeprefix('email:"').removesuffix('"').lower()
            return {"customers": {"nodes": [
                {"id": gid, "email": email} for gid, email in self.customers.items() if email.lower() == wanted
            ]}}
        raise AssertionError(f"Unexpected Shopify call: {query}")

    def codes(self, customer_id):
        return json.loads(self.metafields.get((customer_id, ShopifyDiscountService.CODES_KEY), "[]"))

    def discounts_of(self, customer_id):
        """The customer's discounts as Shopify would apply them: (percentage, minimum or None, code)."""
        found = []
        for discount in self.discounts.values():
            if customer_id in discount["customers"]:
                given = discount["input"]
                minimum = given.get("minimumRequirement", {}).get("subtotal", {}).get("greaterThanOrEqualToSubtotal")
                if "collections" in given["customerGets"]["items"]:
                    minimum = None
                found.append((round(given["customerGets"]["value"]["percentage"] * 100, 2), minimum, given["code"]))
        return sorted(found, key=lambda d: (d[1] is not None, Decimal(d[1] or 0)))


class ClientDiscountSyncTests(TestCase):
    TIERS = (
        {"name": "Tramo 2%", "percentage": 2, "mode": DiscountMode.ACCUMULATED, "level": DiscountLevel.LINE_ITEMS_BASKET,
         "condition": "accumulated >= 2500 and accumulated <= 5000"},
        {"name": "Tramo 3%", "percentage": 3, "mode": DiscountMode.ACCUMULATED, "level": DiscountLevel.LINE_ITEMS_BASKET,
         "condition": "accumulated >= 5000 and accumulated <= 10000"},
        {"name": "Tramo 5%", "percentage": 5, "mode": DiscountMode.ACCUMULATED, "level": DiscountLevel.LINE_ITEMS_BASKET,
         "condition": "accumulated >= 10000"},
    )

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.store = ShopifyStore.objects.create(
            company=cls.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
            scopes=SCOPES, sync_client_discounts=True,
        )
        cls.distributor = make_discount_policy(
            company=cls.company, name="Distribuidor Grande",
            rules=({"name": "Descuento 35", "percentage": 35, "level": DiscountLevel.LINE_ITEM}, *cls.TIERS),
        )
        cls.special = make_discount_policy(
            company=cls.company, name="Descuento 54%",
            rules=({"name": "54%", "percentage": 54, "level": DiscountLevel.LINE_ITEM},),
        )
        cls.links = {}
        for n, (name, policy) in enumerate([("Ana", cls.distributor), ("Bea", cls.distributor), ("Carla", cls.special)], 1):
            client = make_client(company=cls.company, name=name)
            client.discount_policies.add(policy)
            cls.links[name] = ShopifyCustomerLink.objects.create(
                store=cls.store, customer_id=f"gid://shopify/Customer/{n}", client=client, web_store_access=True,
            )
        # A web customer without client prices is never offered anything.
        cls.links["Retail"] = ShopifyCustomerLink.objects.create(
            store=cls.store, customer_id="gid://shopify/Customer/9", client=cls.links["Ana"].client,
        )

    def setUp(self):
        cache.clear()
        self.shopify = FakeShopify()
        patcher = mock.patch.object(ShopifyClient, "graphql", side_effect=self.shopify)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self):
        self.store.refresh_from_db()
        return ShopifyDiscountService.sync(self.store)

    def customer(self, name):
        return self.links[name].customer_id

    def test_customers_get_their_clients_discounts_as_codes(self):
        result = self.sync()

        self.assertEqual(result.offered, 3)
        self.assertEqual(
            [(pct, minimum) for pct, minimum, _code in self.shopify.discounts_of(self.customer("Ana"))],
            [(35.0, None), (2.0, "2500.00"), (3.0, "5000.00"), (5.0, "10000.00")],
        )
        self.assertEqual(
            [(pct, minimum) for pct, minimum, _code in self.shopify.discounts_of(self.customer("Carla"))], [(54.0, None)]
        )
        self.assertEqual(self.shopify.discounts_of(self.customer("Retail")), [])
        # One profile per distinct set of discounts; its codes go to its customers for the theme.
        self.assertEqual(ShopifyDiscountProfile.objects.filter(store=self.store).count(), 2)
        ana_codes = self.shopify.codes(self.customer("Ana"))
        self.assertEqual(ana_codes, self.shopify.codes(self.customer("Bea")))
        self.assertEqual(sorted(ana_codes), sorted(code for _pct, _min, code in self.shopify.discounts_of(self.customer("Ana"))))
        self.assertTrue(all(len(code) == 16 and code.isalnum() for code in ana_codes))
        self.assertEqual(self.shopify.metafields[(self.customer("Carla"), "client_product_discount")], "54.00")
        self.assertEqual(self.shopify.codes(self.customer("Retail")), [])

    def test_product_discount_then_the_best_tier_as_on_a_quote(self):
        self.sync()

        inputs = [d["input"] for d in self.shopify.discounts.values() if self.customer("Ana") in d["customers"]]
        product = next(i for i in inputs if "collections" in i["customerGets"]["items"])
        tiers = [i for i in inputs if i["customerGets"]["items"] == {"all": True}]
        # Product discount on every priced product, combinable with the order (tier) discounts.
        self.assertEqual(product["customerGets"]["items"]["collections"]["add"], [self.store_collection()])
        self.assertEqual(product["combinesWith"], {"productDiscounts": False, "orderDiscounts": True, "shippingDiscounts": True})
        self.assertNotIn("minimumRequirement", product)
        # Tiers combine with the product discount but never with each other: Shopify keeps the best one.
        self.assertEqual(len(tiers), 3)
        for tier in tiers:
            self.assertEqual(tier["combinesWith"], {"productDiscounts": True, "orderDiscounts": False, "shippingDiscounts": True})

    def store_collection(self):
        self.store.refresh_from_db()
        return self.store.discount_collection_id

    def test_titles_customers_see_are_in_the_erps_language_whoever_runs_the_sync(self):
        with translation.override("en"):  # e.g. a staff member using the admin in English
            self.sync()

        with translation.override("es"):
            expected = translation.gettext("Client discount %(percentage)s%%") % {"percentage": "54"}
        titles = {d["input"]["title"] for d in self.shopify.discounts.values() if self.customer("Carla") in d["customers"]}
        self.assertEqual(titles, {expected})

    def test_a_second_run_sends_nothing(self):
        self.sync()
        self.shopify.mutations.clear()

        result = self.sync()

        self.assertEqual(self.shopify.mutations, [])
        self.assertEqual(result.changed, 0)

    def test_changing_a_clients_policy_moves_the_customer(self):
        self.sync()
        ana = self.links["Ana"].client
        ana.discount_policies.set([self.special])

        self.sync()

        self.assertEqual([pct for pct, _min, _code in self.shopify.discounts_of(self.customer("Ana"))], [54.0])
        self.assertEqual(self.shopify.codes(self.customer("Ana")), self.shopify.codes(self.customer("Carla")))
        # Bea still has the distributor profile, so it stays.
        self.assertEqual(len(self.shopify.discounts_of(self.customer("Bea"))), 4)

        self.links["Bea"].client.discount_policies.set([self.special])
        self.sync()

        # Nobody has the distributor discounts any more: they are deleted in Shopify.
        self.assertEqual(len(self.shopify.discounts), 1)
        self.assertEqual(ShopifyDiscountProfile.objects.filter(store=self.store).count(), 1)

    def test_a_rule_change_replaces_the_discounts(self):
        self.sync()
        old_codes = self.shopify.codes(self.customer("Carla"))
        self.special.rules.update(percentage=Decimal("50"))

        self.sync()

        self.assertEqual([pct for pct, _min, _code in self.shopify.discounts_of(self.customer("Carla"))], [50.0])
        self.assertNotEqual(self.shopify.codes(self.customer("Carla")), old_codes)
        self.assertNotIn(old_codes[0], [d["input"]["code"] for d in self.shopify.discounts.values()])

    def test_taking_web_store_access_away_withdraws_the_codes(self):
        self.sync()
        ShopifyCustomerLink.objects.filter(pk=self.links["Carla"].pk).update(web_store_access=False)

        self.sync()

        self.assertEqual(self.shopify.discounts_of(self.customer("Carla")), [])
        self.assertEqual(self.shopify.codes(self.customer("Carla")), [])
        self.assertNotIn((self.customer("Carla"), "client_product_discount"), self.shopify.metafields)
        self.assertIsNone(ShopifyCustomerLink.objects.get(pk=self.links["Carla"].pk).discount_profile)

    def test_turning_the_store_off_withdraws_everything(self):
        self.sync()
        ShopifyStore.objects.filter(pk=self.store.pk).update(sync_client_discounts=False)

        result = self.sync()

        self.assertEqual(result.offered, 0)
        self.assertEqual(self.shopify.discounts, {})
        self.assertEqual(self.shopify.metafields, {})
        self.assertFalse(ShopifyDiscountProfile.objects.filter(store=self.store).exists())

    def test_thresholds_include_tax_where_prices_do(self):
        self.shopify.taxes_included = True
        iva = TaxType.objects.create(tax_name="IVA")
        CompanyDefaultTax.objects.create(company=self.company, tax_type=iva, selected_percentage=Decimal("21"))

        self.sync()

        self.assertEqual(
            [minimum for _pct, minimum, _code in self.shopify.discounts_of(self.customer("Ana"))],
            [None, "3025.00", "6050.00", "12100.00"],
        )

    def test_without_default_taxes_tiers_are_not_guessed(self):
        self.shopify.taxes_included = True

        result = self.sync()

        # The distributors get nothing rather than wrong thresholds; the flat discount needs none.
        self.assertEqual(self.shopify.discounts_of(self.customer("Ana")), [])
        self.assertEqual([pct for pct, _min, _code in self.shopify.discounts_of(self.customer("Carla"))], [54.0])
        link = ShopifyCustomerLink.objects.get(pk=self.links["Ana"].pk)
        self.assertEqual(link.discount_error, gettext(
            "The store's prices include tax, but the company has no default taxes to add to the volume discount thresholds."
        ))
        self.assertEqual(len(result.errors), 2)

    def test_policies_a_store_cannot_apply_are_reported_not_approximated(self):
        self.special.rules.update(condition="subtotal > 1000")

        self.sync()

        self.assertEqual(self.shopify.discounts_of(self.customer("Carla")), [])
        self.assertIn("54%", ShopifyCustomerLink.objects.get(pk=self.links["Carla"].pk).discount_error)

    def test_a_creation_that_fails_half_way_is_replaced_next_time(self):
        self.shopify.fail_on_create = 3

        with self.assertRaises(ShopifyAPIError):
            self.sync()
        half_made = set(self.shopify.discounts)
        self.assertEqual(len(half_made), 2)

        self.sync()

        self.assertFalse(half_made & set(self.shopify.discounts))  # the half-made ones are deleted
        self.assertEqual(len(self.shopify.discounts_of(self.customer("Ana"))), 4)

    def test_an_interrupted_run_is_finished_by_the_next(self):
        self.shopify.fail_metafields = True
        with self.assertRaises(ShopifyAPIError):
            self.sync()
        self.assertEqual(self.shopify.codes(self.customer("Ana")), [])

        self.shopify.fail_metafields = False
        self.sync()

        self.assertEqual(len(self.shopify.codes(self.customer("Ana"))), 4)

    def test_an_interrupted_run_never_leaves_a_revoked_customer_eligible(self):
        # Shopify opened the discounts to Ana, then the run stopped before her codes were written.
        self.shopify.fail_metafields = True
        with self.assertRaises(ShopifyAPIError):
            self.sync()
        self.shopify.fail_metafields = False
        ShopifyCustomerLink.objects.filter(pk=self.links["Ana"].pk).update(web_store_access=False)

        self.sync()

        self.assertEqual(self.shopify.discounts_of(self.customer("Ana")), [])
        self.assertEqual(len(self.shopify.discounts_of(self.customer("Bea"))), 4)

    def test_a_collection_deleted_in_shopify_is_recreated(self):
        self.sync()
        self.shopify.collections.clear()

        self.sync()

        product = next(
            d["input"] for d in self.shopify.discounts.values()
            if self.customer("Carla") in d["customers"] and "collections" in d["input"]["customerGets"]["items"]
        )
        self.assertEqual(product["customerGets"]["items"]["collections"]["add"], [self.store_collection()])
        self.assertIn(self.store_collection(), self.shopify.collections)

    def test_a_discount_deleted_by_hand_does_not_block_withdrawing(self):
        self.sync()
        self.shopify.discounts.clear()
        ShopifyStore.objects.filter(pk=self.store.pk).update(sync_client_discounts=False)

        self.sync()

        self.assertFalse(ShopifyDiscountProfile.objects.filter(store=self.store).exists())

    def test_customers_are_added_in_batches(self):
        with mock.patch.object(ShopifyDiscountService, "CUSTOMER_BATCH", 1):
            self.sync()

        self.assertEqual(len(self.shopify.discounts_of(self.customer("Bea"))), 4)
        self.assertEqual(len(self.shopify.discounts_of(self.customer("Ana"))), 4)

    def test_stores_connected_without_the_new_permissions_are_told_to_reconnect(self):
        ShopifyStore.objects.filter(pk=self.store.pk).update(scopes="read_customers")

        with self.assertRaises(ShopifyAuthError):
            self.sync()
        self.assertEqual(self.shopify.mutations, [])

    def test_one_run_at_a_time(self):
        cache.add(ShopifyDiscountService.SYNC_LOCK.format(store_id=self.store.pk), True)

        with self.assertRaises(ShopifyBusy):
            self.sync()


class DiscountSyncSchedulingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # The fixtures' own saves would leave a pending sync in the class
        # transaction, which never commits; the tests start without one.
        with mock.patch.object(ShopifyPushScheduler, "schedule_discount_sync"):
            cls.company = make_company(name="Fistex")
            cls.store = ShopifyStore.objects.create(
                company=cls.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
                scopes=SCOPES, sync_client_discounts=True,
            )
            cls.policy = make_discount_policy(
                company=cls.company, rules=({"name": "35", "percentage": 35, "level": DiscountLevel.LINE_ITEM},)
            )
            cls.client_ = make_client(company=cls.company)

    def setUp(self):
        cache.clear()
        patcher = mock.patch("apps.shopify.tasks.sync_client_discounts.delay")
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_queues_one_sync(self, change):
        with self.captureOnCommitCallbacks(execute=True):
            change()
            change()  # a second change in the same transaction joins the same sync

        self.delay.assert_called_once_with([self.company.pk])

    def test_saving_a_policy_queues_a_sync(self):
        self.assert_queues_one_sync(self.policy.save)

    def test_changing_a_rule_queues_a_sync(self):
        self.assert_queues_one_sync(self.policy.rules.first().save)

    def test_changing_a_clients_policies_queues_a_sync(self):
        def change() -> None:
            self.client_.discount_policies.add(self.policy)
            self.client_.discount_policies_changed()  # as the client form and the company repair do

        self.assert_queues_one_sync(change)

    def test_changing_the_default_taxes_queues_a_sync(self):
        iva = TaxType.objects.create(tax_name="IVA")
        self.assert_queues_one_sync(
            lambda: CompanyDefaultTax.objects.update_or_create(
                company=self.company, tax_type=iva, defaults={"selected_percentage": Decimal("21")}
            )
        )

    def test_turning_the_store_on_queues_a_sync_but_token_refreshes_do_not(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.store.save()
        self.delay.assert_called_once_with([self.company.pk])

        self.delay.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            self.store.save(update_fields=["access_token_encrypted", "updated_at"])
        self.delay.assert_not_called()

    def test_nothing_is_queued_while_no_store_offers_client_discounts(self):
        ShopifyStore.objects.filter(pk=self.store.pk).update(sync_client_discounts=False)

        with self.captureOnCommitCallbacks(execute=True):
            self.policy.save()

        self.delay.assert_not_called()

    def test_a_busy_store_makes_the_task_try_again(self):
        with (
            mock.patch.object(ShopifyDiscountService, "sync", side_effect=ShopifyBusy("busy")),
            mock.patch.object(sync_client_discounts, "retry", side_effect=RuntimeError("retry")) as retry,
        ):
            with self.assertRaises(RuntimeError):
                sync_client_discounts.run([self.company.pk])
        retry.assert_called_once()

    def test_scheduler_ignores_missing_company(self):
        with self.captureOnCommitCallbacks(execute=True):
            ShopifyPushScheduler.schedule_discount_sync([None])

        self.delay.assert_not_called()


class ClientAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with mock.patch.object(ShopifyPushScheduler, "schedule_discount_sync"):
            cls.company = make_company(name="Fistex")
            cls.other = make_company(name="Other")
            cls.store = ShopifyStore.objects.create(
                company=cls.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
                scopes=SCOPES, sync_client_discounts=True,
            )
            cls.other_store = ShopifyStore.objects.create(
                company=cls.other, name="Other shop", shop_domain="other.myshopify.com", access_token_encrypted="x",
                scopes=SCOPES,
            )
            cls.ana = make_client(company=cls.company, name="Instalaciones Ana", email="ana@example.com")
            cls.bea = make_client(company=cls.company, name="Bea", email="")
            cls.foreign = make_client(company=cls.other, name="Foreign", email="foreign@example.com")
        cls.user = make_user(username="staff")
        cls.user.is_staff = True
        cls.user.save()
        cls.user.user_permissions.add(
            *Permission.objects.filter(codename__in=["view_shopifystore", "change_shopifystore",
                                                     "view_shopifycustomerlink", "change_shopifycustomerlink"])
        )
        CompanyUser.objects.create(user=cls.user, company=cls.company, is_active=True)

    def setUp(self):
        cache.clear()
        self.shopify = FakeShopify()
        patcher = mock.patch.object(ShopifyClient, "graphql", side_effect=self.shopify)
        patcher.start()
        self.addCleanup(patcher.stop)
        scheduler = mock.patch.object(ShopifyPushScheduler, "schedule_discount_sync")
        self.schedule = scheduler.start()
        self.addCleanup(scheduler.stop)

    def give(self, *clients):
        return ShopifyDiscountService.give_web_store_access(self.store, clients)

    def test_a_new_customer_is_created_with_the_clients_email(self):
        result = self.give(self.ana)

        self.assertEqual(result.given, ["Instalaciones Ana"])
        link = ShopifyCustomerLink.objects.get(store=self.store, client=self.ana)
        self.assertTrue(link.web_store_access)
        self.assertEqual(self.shopify.customers[link.customer_id], "ana@example.com")
        self.schedule.assert_called_once_with({self.company.pk})

    def test_an_existing_customer_with_the_email_is_used(self):
        self.shopify.customers["gid://shopify/Customer/7"] = "ANA@example.com"

        self.give(self.ana)

        self.assertNotIn("customerCreate", self.shopify.mutations)
        self.assertEqual(ShopifyCustomerLink.objects.get(client=self.ana).customer_id, "gid://shopify/Customer/7")

    def test_a_customer_linked_by_web_orders_keeps_its_link(self):
        self.shopify.customers["gid://shopify/Customer/7"] = "ana@example.com"
        ShopifyCustomerLink.objects.create(store=self.store, customer_id="gid://shopify/Customer/7", client=self.ana)

        self.give(self.ana)

        self.assertEqual(ShopifyCustomerLink.objects.filter(client=self.ana).count(), 1)
        self.assertTrue(ShopifyCustomerLink.objects.get(client=self.ana).web_store_access)

    def test_clients_that_cannot_sign_in_as_themselves_are_refused(self):
        twin = make_client(company=self.company, name="Twin", email="twin@example.com")
        make_client(company=self.company, name="Twin 2", email="TWIN@example.com")
        taken = make_client(company=self.company, name="Taken", email="taken@example.com")
        self.shopify.customers["gid://shopify/Customer/8"] = "taken@example.com"
        ShopifyCustomerLink.objects.create(store=self.store, customer_id="gid://shopify/Customer/8", client=self.ana)

        result = self.give(self.bea, twin, taken, self.foreign)

        self.assertEqual(result.given, [])
        self.assertEqual(len(result.errors), 4)
        self.assertIn(gettext("The client has no email to sign in with."), result.errors[0])
        self.assertIn(gettext("Another client has the same email, and Shopify allows one customer per email."), result.errors[1])
        self.assertIn("Instalaciones Ana", result.errors[2])
        self.assertIn(gettext("The client belongs to another company."), result.errors[3])
        self.assertFalse(ShopifyCustomerLink.objects.filter(web_store_access=True).exists())
        self.assertNotIn("customerCreate", self.shopify.mutations)

    def test_stores_connected_without_the_new_permissions_are_told_to_reconnect(self):
        ShopifyStore.objects.filter(pk=self.store.pk).update(scopes="read_customers")
        self.store.refresh_from_db()

        with self.assertRaises(ShopifyAuthError):
            self.give(self.ana)

    def test_removing_web_store_access_schedules_their_withdrawal(self):
        self.give(self.ana)
        self.schedule.reset_mock()

        removed = ShopifyDiscountService.set_web_store_access(ShopifyCustomerLink.objects.filter(client=self.ana), False)

        self.assertEqual(removed, 1)
        self.assertFalse(ShopifyCustomerLink.objects.get(client=self.ana).web_store_access)
        self.schedule.assert_called_once_with({self.company.pk})

    # ── Through the admin ────────────────────────────────────────────────

    def test_staff_choose_the_clients_on_the_stores_page(self):
        self.client.force_login(self.user)
        url = reverse("admin:shopify_shopifystore_web_store_access_form", args=[self.store.pk])

        button = self.client.get(reverse("admin:shopify_shopifystore_give_web_store_access", args=[self.store.pk]))
        page = self.client.get(url)
        response = self.client.post(url, {"clients": [self.ana.pk]})

        self.assertRedirects(button, url, fetch_redirect_response=False)

        self.assertContains(page, "Instalaciones Ana")
        self.assertNotContains(page, "Foreign")  # only the store's company's clients are offered
        self.assertRedirects(
            response, reverse("admin:shopify_shopifycustomerlink_changelist") + f"?store__id__exact={self.store.pk}",
            fetch_redirect_response=False,
        )
        self.assertTrue(ShopifyCustomerLink.objects.get(client=self.ana).web_store_access)

    def test_a_client_of_another_company_is_rejected_not_just_hidden(self):
        self.client.force_login(self.user)
        url = reverse("admin:shopify_shopifystore_web_store_access_form", args=[self.store.pk])

        response = self.client.post(url, {"clients": [self.foreign.pk]})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertFalse(ShopifyCustomerLink.objects.exists())

    def test_another_companys_store_cannot_be_used(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("admin:shopify_shopifystore_web_store_access_form", args=[self.other_store.pk]))

        self.assertEqual(response.status_code, 404)

    def test_staff_without_permission_cannot_give_access(self):
        self.user.user_permissions.remove(Permission.objects.get(codename="change_shopifystore"))
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("admin:shopify_shopifystore_web_store_access_form", args=[self.store.pk]), {"clients": [self.ana.pk]}
        )

        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(ShopifyCustomerLink.objects.exists())

    def test_web_store_access_is_removed_from_the_customer_list(self):
        link = ShopifyCustomerLink.objects.create(
            store=self.store, customer_id="gid://shopify/Customer/7", client=self.ana, web_store_access=True
        )
        self.client.force_login(self.user)

        self.client.post(
            reverse("admin:shopify_shopifycustomerlink_changelist"),
            {"action": "remove_web_store_access", "_selected_action": [link.pk]},
        )

        link.refresh_from_db()
        self.assertFalse(link.web_store_access)
