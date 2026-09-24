"""
Delivery orders built from quotes with negative lines, the value remainder
carried by the order that completes a partially delivered line, and the
superuser-only "Sync from Quote" action.

    python manage.py test invoices.tests.test_delivery_order_sync
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from company.models import Company, CompanyUser
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from inventory.models import InventoryItem
from invoices.models import AppliedTax, TaxType
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.views.invoice.delivery_order_wizard_views import DeliveryOrderWizardView
from management.models import ProjectFile
from sales.models import (
    Client,
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    Quote,
    QuoteInventoryItem,
    QuoteStatus,
)


def _basket_entry(base, amount, percentage="10.00"):
    return {
        "rule_name": f"{percentage}%", "kind": "standard", "level": "basket",
        "mode": "net", "percentage": percentage,
        "base": str(base), "amount": str(amount),
    }


class _SyncTestBase(TestCase):

    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(name="Door", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF Sync", company=self.company, client=self.client_obj,
        )
        self.iva21, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"},
        )
        self.config_ct = ContentType.objects.get_for_model(Configuration)
        self.inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

    # ── factories ─────────────────────────────────────────────────────

    def _quote(self, subtotal, discount=Decimal("0.00")):
        """An accepted quote whose stored totals are set by hand (a basket
        discount of ``discount`` on ``subtotal``, taxed at 21%)."""
        quote = Quote.objects.create(
            project_name="Quote", company=self.company, client=self.client_obj,
            project_file=self.project_file, status=QuoteStatus.ACCEPTED,
        )
        self._set_quote_totals(quote, subtotal, discount)
        return quote

    def _set_quote_totals(self, quote, subtotal, discount=Decimal("0.00")):
        """Writes the quote's totals without ``save()``, so the recalculation
        (which knows no discount policy here) does not undo them."""
        subtotal, discount = Decimal(subtotal), Decimal(discount)
        discounted = subtotal - discount
        tax = (discounted * Decimal("0.21")).quantize(Decimal("0.01"))
        Quote.objects.filter(pk=quote.pk).update(
            subtotal_price=subtotal,
            discount_amount=discount,
            discounted_subtotal=discounted,
            tax_amount=tax,
            total_price=discounted + tax,
            discount_breakdown=[_basket_entry(subtotal, discount)] if discount else [],
        )
        quote.refresh_from_db()

    def _tax(self, content_type, obj, base):
        AppliedTax.objects.create(
            content_type=content_type, object_id=obj.pk, tax_type=self.iva21,
            selected_percentage=Decimal("21"), base_amount=base,
            calculated_amount=(Decimal(base) * Decimal("0.21")).quantize(Decimal("0.01")),
        )

    def _config(self, quote, subtotal, units=Decimal("0")):
        config = Configuration.objects.create(
            name="Config", company=self.company, product=self.product,
            client=self.client_obj, quote=quote, project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=subtotal, total_price=Decimal(subtotal) * Decimal("1.21"),
            units=units,
        )
        self._tax(self.config_ct, config, subtotal)
        return config

    def _reprice_config(self, config, subtotal):
        Configuration.objects.filter(pk=config.pk).update(subtotal_price=Decimal(subtotal))
        config.refresh_from_db()

    def _inv_line(self, quote, quantity, unit_price, discount=Decimal("0")):
        item = InventoryItem.objects.create(
            company=self.company, name="Hinge",
            code=f"HNG-{InventoryItem.objects.count() + 1}",
        )
        line = QuoteInventoryItem.objects.create(
            quote=quote, inventory_item=item, quantity=Decimal(quantity),
            unit_price=Decimal(unit_price), discount=discount,
        )
        self._tax(self.inv_item_ct, line, line.line_total.amount)
        return line

    def _line_data(self, source, quantity, unit_price, discount=Decimal("0.00")):
        if isinstance(source, Configuration):
            ct, description = self.config_ct, source.name
        else:
            ct, description = self.inv_item_ct, source.inventory_item.name
        return {
            "source_content_type_id": ct.id,
            "source_object_id": source.pk,
            "description": description,
            "quantity": Decimal(quantity),
            "unit_price": Decimal(unit_price),
            "discount_percentage": discount,
        }

    def _create_do(self, items_data):
        return DeliveryOrderService.create_delivery_order(
            project_file=self.project_file, company=self.company,
            client=self.client_obj, items_data=items_data,
        )

    def _deliverable(self):
        return {
            (i["source_content_type_id"], i["source_object_id"]): i
            for i in DeliveryOrderService.get_deliverable_items(self.project_file)
        }

    def _wizard_initial(self):
        view = DeliveryOrderWizardView()
        view.project_file = self.project_file
        view.delivery_summary = DeliveryOrderService.get_delivery_summary(self.project_file)
        return view._items_initial()


class NegativeQuoteLineTests(_SyncTestBase):
    """A quote with a deduction line: 1000 configuration − 100 inventory
    line = 900, 10% basket discount → 810 taxable, 170.10 IVA, 980.10."""

    def setUp(self):
        super().setUp()
        self.quote = self._quote("0")
        self.config = self._config(self.quote, Decimal("1000.00"))
        self.deduction = self._inv_line(self.quote, "-1", "100.00")
        self._set_quote_totals(self.quote, "900.00", "90.00")

    def _both_lines(self):
        items = self._deliverable()
        return [
            self._line_data(self.config, "1", "1000.00"),
            self._line_data(
                self.deduction,
                items[(self.inv_item_ct.id, self.deduction.pk)]["remaining_quantity"],
                items[(self.inv_item_ct.id, self.deduction.pk)]["unit_price"],
            ),
        ]

    def test_negative_line_is_offered_for_delivery(self):
        items = self._deliverable()
        self.assertIn((self.inv_item_ct.id, self.deduction.pk), items)
        deduction = items[(self.inv_item_ct.id, self.deduction.pk)]
        self.assertEqual(deduction["remaining_quantity"], Decimal("-1"))
        self.assertEqual(deduction["unit_price"], Decimal("100.00"))

    def test_wizard_prefills_the_negative_line(self):
        quantities = [row["quantity"] for row in self._wizard_initial()]
        self.assertIn(Decimal("-1"), quantities)

    def test_delivery_order_matches_the_quote(self):
        do = self._create_do(self._both_lines())
        self.assertEqual(do.subtotal_amount.amount, Decimal("900.00"))
        self.assertEqual(do.discount_amount.amount, Decimal("90.00"))
        self.assertEqual(do.applied_taxes.get().base_amount.amount, Decimal("810.00"))
        self.assertEqual(do.tax_amount.amount, self.quote.tax_amount.amount)
        self.assertEqual(do.total_amount.amount, self.quote.total_price.amount)

    def test_invoice_from_the_order_matches_the_quote(self):
        do = self._create_do(self._both_lines())
        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [do], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 24),
        )
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("900.00"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("90.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("980.10"))

    def test_delivered_negative_line_is_not_offered_again(self):
        self._create_do(self._both_lines())
        self.assertEqual(self._deliverable(), {})

    def test_negative_unit_price_line_is_taxed_on_its_discounted_base(self):
        self.deduction.delete()
        credit = self._inv_line(self.quote, "1", "-100.00")
        self._set_quote_totals(self.quote, "900.00", "90.00")

        do = self._create_do([
            self._line_data(self.config, "1", "1000.00"),
            self._line_data(credit, "1", "-100.00"),
        ])
        self.assertEqual(do.discount_amount.amount, Decimal("90.00"))
        self.assertEqual(do.applied_taxes.get().base_amount.amount, Decimal("810.00"))
        self.assertEqual(do.total_amount.amount, Decimal("980.10"))


class RemainderPricingTests(_SyncTestBase):
    """The order that ships the rest of a line carries the rest of its value."""

    def test_quote_repriced_after_a_partial_delivery(self):
        quote = self._quote("1000.00")
        config = self._config(quote, "1000.00", units=Decimal("2"))
        self._create_do([self._line_data(config, "1", "500.00")])

        self._reprice_config(config, "1200.00")
        self._set_quote_totals(quote, "1200.00")

        rest = self._deliverable()[(self.config_ct.id, config.pk)]
        self.assertEqual(rest["remaining_quantity"], Decimal("1"))
        self.assertEqual(rest["unit_price"], Decimal("700.00"))

        do2 = self._create_do([self._line_data(config, "1", rest["unit_price"])])
        self.assertEqual(do2.subtotal_amount.amount, Decimal("700.00"))

    def test_line_split_by_price_rather_than_quantity(self):
        quote = self._quote("1000.00")
        config = self._config(quote, "1000.00")
        # Half the configuration, but priced at 600 by hand on the first order.
        self._create_do([self._line_data(config, "0.5", "1200.00")])

        rest = self._deliverable()[(self.config_ct.id, config.pk)]
        self.assertEqual(rest["remaining_quantity"], Decimal("0.5"))
        self.assertEqual(rest["unit_price"], Decimal("800.00"))  # 400 left

    def test_untouched_quote_keeps_the_plain_unit_price(self):
        quote = self._quote("1000.00")
        config = self._config(quote, "1000.00", units=Decimal("2"))
        self._create_do([self._line_data(config, "1", "500.00")])

        rest = self._deliverable()[(self.config_ct.id, config.pk)]
        self.assertEqual(rest["unit_price"], Decimal("500.00"))

    def test_nothing_delivered_yet_uses_the_quote_price(self):
        quote = self._quote("1000.00")
        config = self._config(quote, "1000.00", units=Decimal("4"))
        self.assertEqual(
            self._deliverable()[(self.config_ct.id, config.pk)]["unit_price"],
            Decimal("250.00"),
        )

    def test_inventory_line_with_a_line_discount(self):
        quote = self._quote("360.00")
        line = self._inv_line(quote, "4", "100.00", discount=Decimal("10"))  # 360 net
        self._create_do([self._line_data(line, "1", "100.00", Decimal("10"))])  # 90

        QuoteInventoryItem.objects.filter(pk=line.pk).update(
            unit_price=Decimal("130.00"), line_total=Decimal("468.00"),
        )
        self._set_quote_totals(quote, "468.00")

        rest = self._deliverable()[(self.inv_item_ct.id, line.pk)]
        self.assertEqual(rest["remaining_quantity"], Decimal("3"))
        self.assertEqual(rest["discount_percentage"], Decimal("10"))
        self.assertEqual(rest["unit_price"], Decimal("140.00"))  # 3 × 140 × 0.9 = 378 = 468 − 90

    def test_legacy_net_priced_delivery_counts_at_the_quote_price(self):
        quote = self._quote("1000.00", "100.00")
        config = self._config(quote, "1000.00", units=Decimal("2"))
        legacy = DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, status=DeliveryOrderStatus.SENT,
        )
        DeliveryOrderItem.objects.create(
            delivery_order=legacy, description="Config (net)",
            quantity=Decimal("1"), unit_price=Decimal("450.00"),
            source_content_type=self.config_ct, source_object_id=config.pk,
        )

        rest = self._deliverable()[(self.config_ct.id, config.pk)]
        self.assertEqual(rest["remaining_quantity"], Decimal("1"))
        self.assertEqual(rest["unit_price"], Decimal("500.00"))


class ManualLineTaxTests(_SyncTestBase):
    """A line added by hand (portes) is taxed like the quote would tax it.
    Production case DO-2026-00050: 3532.61 goods − 70.65 discount + 260
    portes = 3721.96 taxable, 781.61 IVA, 4503.57 total."""

    def setUp(self):
        super().setUp()
        self.quote = self._quote("3532.61", "70.65")
        self.config = self._config(self.quote, "3532.61")
        self.do = self._create_do([self._line_data(self.config, "1", "3532.61")])

    def _add_portes(self, **kwargs):
        DeliveryOrderItem.objects.create(
            delivery_order=self.do, description="Portes",
            quantity=Decimal("1"), unit_price=Decimal("260.00"), **kwargs,
        )
        DeliveryOrderService.recompute_discounts_and_taxes(self.do)
        self.do.refresh_from_db()

    def _client_tax_mode(self):
        self.company.auto_apply_client_taxes = True
        self.company.save(update_fields=["auto_apply_client_taxes"])
        self._tax(ContentType.objects.get_for_model(Client), self.client_obj, "0")
        self._tax(ContentType.objects.get_for_model(Quote), self.quote, "3461.96")

    def test_client_tax_mode_taxes_the_manual_line_with_the_client_taxes(self):
        self._client_tax_mode()
        self._add_portes()

        self.assertEqual(self.do.subtotal_amount.amount, Decimal("3792.61"))
        self.assertEqual(self.do.discount_amount.amount, Decimal("70.65"))
        self.assertEqual(self.do.applied_taxes.get().base_amount.amount, Decimal("3721.96"))
        self.assertEqual(self.do.tax_amount.amount, Decimal("781.61"))
        self.assertEqual(self.do.total_amount.amount, Decimal("4503.57"))

    def test_source_mode_uses_the_items_own_taxes(self):
        portes = InventoryItem.objects.create(company=self.company, name="Portes", code="PORTES")
        self._tax(ContentType.objects.get_for_model(InventoryItem), portes, "0")
        self._add_portes(inventory_item=portes)

        self.assertEqual(self.do.applied_taxes.get().base_amount.amount, Decimal("3721.96"))
        self.assertEqual(self.do.total_amount.amount, Decimal("4503.57"))

    def test_source_mode_without_any_taxes_leaves_the_line_untaxed(self):
        self._add_portes()
        self.assertEqual(self.do.applied_taxes.get().base_amount.amount, Decimal("3461.96"))
        self.assertEqual(self.do.total_amount.amount, Decimal("4448.97"))


class SyncFromSourcesTests(_SyncTestBase):

    def setUp(self):
        super().setUp()
        self.quote = self._quote("1000.00", "100.00")
        self.config = self._config(self.quote, "1000.00")
        self.do = self._create_do([self._line_data(self.config, "1", "1000.00")])

    def _config_line(self, do=None):
        return (do or self.do).items.top_level().get(
            source_content_type=self.config_ct, source_object_id=self.config.pk,
        )

    def test_reprices_lines_and_recomputes_the_header(self):
        self._reprice_config(self.config, "1200.00")
        self._set_quote_totals(self.quote, "1200.00", "120.00")

        counts = DeliveryOrderService.sync_from_sources(self.do)
        self.do.refresh_from_db()

        self.assertEqual(counts, {"updated": 1, "added": 0, "removed": 0})
        self.assertEqual(self._config_line().unit_price.amount, Decimal("1200.00"))
        self.assertEqual(self.do.subtotal_amount.amount, Decimal("1200.00"))
        self.assertEqual(self.do.discount_amount.amount, Decimal("120.00"))
        self.assertEqual(self.do.applied_taxes.get().base_amount.amount, Decimal("1080.00"))
        self.assertEqual(self.do.total_amount.amount, Decimal("1306.80"))

    def test_is_idempotent(self):
        # The first pass only normalises the hand-written description and
        # reference to what the wizard writes; nothing changes after that.
        first = DeliveryOrderService.sync_from_sources(self.do)
        second = DeliveryOrderService.sync_from_sources(self.do)
        self.do.refresh_from_db()
        self.assertEqual(first, {"updated": 1, "added": 0, "removed": 0})
        self.assertEqual(second, {"updated": 0, "added": 0, "removed": 0})
        self.assertEqual(self._config_line().description, f"Config (Q#{self.quote.pk})")
        self.assertEqual(self._config_line().reference, f"CFG-{self.config.pk}")
        self.assertEqual(self.do.total_amount.amount, Decimal("1089.00"))

    def test_the_order_completing_a_split_line_gets_the_remainder(self):
        self.do.delete()
        do1 = self._create_do([self._line_data(self.config, "0.5", "1000.00")])
        do2 = self._create_do([self._line_data(self.config, "0.5", "1000.00")])

        self._reprice_config(self.config, "1200.00")
        self._set_quote_totals(self.quote, "1200.00", "120.00")
        DeliveryOrderService.sync_from_sources(do2)
        do1.refresh_from_db()
        do2.refresh_from_db()

        self.assertEqual(self._config_line(do2).quantity, Decimal("0.5"))
        self.assertEqual(self._config_line(do2).line_total.amount, Decimal("700.00"))
        self.assertEqual(do2.discount_amount.amount, Decimal("70.00"))
        self.assertEqual(do1.subtotal_amount.amount, Decimal("500.00"))

    def test_removes_lines_whose_source_left_the_quote(self):
        line = self._inv_line(self.quote, "2", "50.00")
        DeliveryOrderItem.objects.create(
            delivery_order=self.do, description="Hinge", quantity=Decimal("2"),
            unit_price=Decimal("50.00"),
            source_content_type=self.inv_item_ct, source_object_id=line.pk,
        )
        line.delete()
        self._set_quote_totals(self.quote, "1000.00", "100.00")

        counts = DeliveryOrderService.sync_from_sources(self.do)

        self.assertEqual(counts["removed"], 1)
        self.assertEqual(self.do.items.count(), 1)

    def test_adds_lines_the_quote_gained(self):
        line = self._inv_line(self.quote, "2", "50.00")
        self._set_quote_totals(self.quote, "1100.00", "110.00")

        counts = DeliveryOrderService.sync_from_sources(self.do)
        self.do.refresh_from_db()

        self.assertEqual(counts["added"], 1)
        added = self.do.items.get(source_content_type=self.inv_item_ct, source_object_id=line.pk)
        self.assertEqual(added.quantity, Decimal("2"))
        self.assertEqual(added.unit_price.amount, Decimal("50.00"))
        self.assertEqual(self.do.subtotal_amount.amount, Decimal("1100.00"))
        self.assertEqual(self.do.discount_amount.amount, Decimal("110.00"))

    def test_does_not_add_what_other_orders_already_ship(self):
        line = self._inv_line(self.quote, "2", "50.00")
        self._set_quote_totals(self.quote, "1100.00", "110.00")
        self._create_do([self._line_data(line, "2", "50.00")])

        counts = DeliveryOrderService.sync_from_sources(self.do)

        self.assertEqual(counts["added"], 0)
        self.assertEqual(self.do.items.count(), 1)

    def test_keeps_manual_lines_and_shipped_quantities(self):
        DeliveryOrderItem.objects.create(
            delivery_order=self.do, description="Portes",
            quantity=Decimal("1"), unit_price=Decimal("30.00"),
        )
        line = self._config_line()
        line.quantity = Decimal("0.5")
        line.save()

        DeliveryOrderService.sync_from_sources(self.do)
        self.do.refresh_from_db()

        self.assertTrue(self.do.items.filter(description="Portes").exists())
        self.assertEqual(self._config_line().quantity, Decimal("0.5"))
        self.assertEqual(self.do.subtotal_amount.amount, Decimal("530.00"))

    def test_refuses_an_invoiced_order(self):
        DeliveryOrderService.create_invoice_from_delivery_orders(
            [self.do], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 24),
        )
        self.do.refresh_from_db()
        with self.assertRaises(ValidationError):
            DeliveryOrderService.sync_from_sources(self.do)


class SyncFromQuoteAdminActionTests(_SyncTestBase):

    def setUp(self):
        super().setUp()
        self.quote = self._quote("1000.00", "100.00")
        self.config = self._config(self.quote, "1000.00")
        self.do = self._create_do([self._line_data(self.config, "1", "1000.00")])
        self.url = reverse(
            "admin:sales_deliveryorder_sync_from_quote_detail_action", args=[self.do.pk],
        )
        self.change_url = reverse("admin:sales_deliveryorder_change", args=[self.do.pk])

    def _staff_user(self):
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        CompanyUser.objects.create(user=user, company=self.company, is_active=True)
        user.user_permissions.set(
            Permission.objects.filter(content_type__app_label__in=["sales", "invoices"])
        )
        return user

    def test_superuser_syncs_and_is_sent_back_to_the_order(self):
        self.client.force_login(
            User.objects.create_superuser(username="admin", email="a@x.com", password="pw")
        )
        self._reprice_config(self.config, "1200.00")
        self._set_quote_totals(self.quote, "1200.00", "120.00")

        response = self.client.get(self.url)

        self.assertRedirects(response, self.change_url, fetch_redirect_response=False)
        self.do.refresh_from_db()
        self.assertEqual(self.do.total_amount.amount, Decimal("1306.80"))

    def test_superuser_sees_the_action_on_the_change_form(self):
        self.client.force_login(
            User.objects.create_superuser(username="admin", email="a@x.com", password="pw")
        )
        response = self.client.get(self.change_url)
        self.assertContains(response, self.url)

    def test_staff_user_cannot_use_or_see_the_action(self):
        self.client.force_login(self._staff_user())

        self.assertEqual(self.client.get(self.url).status_code, 403)
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url)
