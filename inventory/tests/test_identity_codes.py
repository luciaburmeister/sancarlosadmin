"""
Tests for bulk identity-code registration from the InventoryItem side.

    python manage.py test inventory.tests_identity_codes

Covers:
  • ItemIdentityCodesForm — parsing, dedupe within a paste, cross-line clashes,
    codes already registered
  • ItemIdentityCodesView — creation, receipt attribution, company scoping
  • ItemIdentityCodeInline / DeliveredItemIdentityCodeInline — read-only,
    disjoint querysets, distinct formset prefixes
"""

from datetime import date
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import translation
from djmoney.money import Money

from company.models import Company
from inventory.admin import DeliveredItemIdentityCodeInline, ItemIdentityCodeInline
from inventory.forms import UNLINKED, ItemIdentityCodesForm
from inventory.models import InventoryItem, ItemIdentityCode, Warehouse
from materials.models import Provider
from sales.models import Client, DeliveryOrder, DeliveryOrderItem
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptItem,
    PurchaseOrderStatus,
)


class _IdentityCodesBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co", tax_identifier="B12345678")
        self.provider = Provider.objects.create(company=self.company, name="Proveedor SA")
        self.warehouse = Warehouse.objects.create(company=self.company, code="WH1", name="Central")
        self.item = InventoryItem.objects.create(name="Perfil aluminio", company=self.company)

        self.po = PurchaseOrder.objects.create(
            company=self.company,
            provider=self.provider,
            date_ordered=date(2026, 6, 1),
            status=PurchaseOrderStatus.APPROVED,
        )
        self.po_item = PurchaseOrderItem.objects.create(
            purchase_order=self.po,
            inventory_item=self.item,
            quantity=Decimal("20"),
            unit_price=Money("5.00", "EUR"),
        )
        # Two receipt lines for the same item, so the form renders two textareas.
        self.receipt_a = PurchaseOrderReceipt.objects.create(
            company=self.company, purchase_order=self.po, warehouse=self.warehouse,
        )
        self.ri_a = PurchaseOrderReceiptItem.objects.create(
            receipt=self.receipt_a, po_item=self.po_item, quantity_received=Decimal("10"),
        )
        self.receipt_b = PurchaseOrderReceipt.objects.create(
            company=self.company, purchase_order=self.po, warehouse=self.warehouse,
        )
        self.ri_b = PurchaseOrderReceiptItem.objects.create(
            receipt=self.receipt_b, po_item=self.po_item, quantity_received=Decimal("10"),
        )

    def _request(self):
        """Inline.get_queryset() runs admin permission checks, so it needs a
        request carrying a user."""
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser(
            username=f"su{User.objects.count()}", email="s@u.c", password="pass12345",
        )
        return request

    def _form(self, **codes):
        """Build a bound form; pass codes_a=..., codes_b=... as raw textarea text."""
        data = {}
        if "a" in codes:
            data[f"codes_{self.ri_a.id}"] = codes["a"]
        if "b" in codes:
            data[f"codes_{self.ri_b.id}"] = codes["b"]
        return ItemIdentityCodesForm(data=data, item=self.item)


class ItemIdentityCodesFormTest(_IdentityCodesBase):

    def test_renders_one_textarea_per_receipt_line(self):
        form = ItemIdentityCodesForm(item=self.item)
        self.assertEqual(len(form.receipt_items), 2)
        self.assertIn(f"codes_{self.ri_a.id}", form.fields)
        self.assertIn(f"codes_{self.ri_b.id}", form.fields)

    def test_no_receipt_lines_for_unreceived_item(self):
        other = InventoryItem.objects.create(name="Tornillo", company=self.company)
        form = ItemIdentityCodesForm(item=other)
        self.assertEqual(form.receipt_items, [])

    def test_parses_one_code_per_line_ignoring_blanks_and_whitespace(self):
        form = self._form(a="SN-001\n  SN-002  \n\n\nSN-003\n")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.parsed_codes, {self.ri_a.id: ["SN-001", "SN-002", "SN-003"]})

    def test_splits_codes_across_receipt_lines(self):
        form = self._form(a="SN-001", b="SN-002")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.parsed_codes,
            {self.ri_a.id: ["SN-001"], self.ri_b.id: ["SN-002"]},
        )

    def test_collapses_a_code_repeated_within_one_textarea(self):
        form = self._form(a="SN-001\nSN-001\nSN-002")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.parsed_codes, {self.ri_a.id: ["SN-001", "SN-002"]})

    def test_rejects_the_same_code_under_two_receipt_lines(self):
        """Every line here is the same item, so this would violate
        uniq_identity_code_per_item. The PO-side form can't hit this.

        Receipt lines render newest-first, and the first one to claim a code
        keeps it — so the error lands on whichever line sorts later, not on a
        fixed one."""
        form = self._form(a="SN-001", b="SN-001")
        self.assertFalse(form.is_valid())

        flagged = [f"codes_{ri.id}" for ri in form.receipt_items
                   if f"codes_{ri.id}" in form.errors]
        self.assertEqual(len(flagged), 1)
        self.assertIn("SN-001", str(form.errors[flagged[0]]))
        # The winning line still parses it, so exactly one row would be created.
        self.assertEqual(sum(len(c) for c in form.parsed_codes.values()), 1)

    def test_reports_already_registered_codes_as_skipped(self):
        ItemIdentityCode.objects.create(item=self.item, code="SN-001")
        form = self._form(a="SN-001\nSN-002")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.parsed_codes, {self.ri_a.id: ["SN-002"]})
        self.assertEqual(form.skipped_codes, ["SN-001"])

    def test_unlinked_textarea_is_always_present(self):
        """Historic uploads need a home even when the item has no receipts."""
        bare = InventoryItem.objects.create(name="Tornillo", company=self.company)
        form = ItemIdentityCodesForm(item=bare)
        self.assertEqual(form.receipt_items, [])
        self.assertIn(ItemIdentityCodesForm.UNLINKED_FIELD, form.fields)

    def test_parses_unlinked_codes_under_the_unlinked_key(self):
        form = ItemIdentityCodesForm(
            data={ItemIdentityCodesForm.UNLINKED_FIELD: "SN-H1\nSN-H2"},
            item=self.item,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.parsed_codes, {UNLINKED: ["SN-H1", "SN-H2"]})

    def test_unlinked_box_is_processed_before_receipt_lines(self):
        """It renders first, so it claims a code entered twice and the receipt
        line is the one flagged."""
        form = ItemIdentityCodesForm(
            data={
                ItemIdentityCodesForm.UNLINKED_FIELD: "SN-001",
                f"codes_{self.ri_a.id}": "SN-001",
            },
            item=self.item,
        )
        self.assertFalse(form.is_valid())
        self.assertIn(f"codes_{self.ri_a.id}", form.errors)
        self.assertNotIn(ItemIdentityCodesForm.UNLINKED_FIELD, form.errors)

    def test_a_code_registered_on_another_item_is_not_skipped(self):
        other = InventoryItem.objects.create(name="Tornillo", company=self.company)
        ItemIdentityCode.objects.create(item=other, code="SN-001")
        form = self._form(a="SN-001")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.parsed_codes, {self.ri_a.id: ["SN-001"]})
        self.assertEqual(form.skipped_codes, [])


class ItemIdentityCodesViewTest(_IdentityCodesBase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username="admin", email="a@b.c", password="pass12345",
        )
        self.client.force_login(self.user)
        self.url = reverse("admin:inventory_item_identity_codes", args=[self.item.pk])

    def test_page_lists_every_receipt_line(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["receipt_items"]), 2)

    def test_posting_codes_creates_them_against_the_receipt_line(self):
        response = self.client.post(self.url, {
            f"codes_{self.ri_a.id}": "SN-001\nSN-002",
            f"codes_{self.ri_b.id}": "SN-003",
        })
        self.assertRedirects(response, self.url)

        codes = ItemIdentityCode.objects.filter(item=self.item).order_by("code")
        self.assertEqual([c.code for c in codes], ["SN-001", "SN-002", "SN-003"])
        self.assertEqual(
            {c.code: c.receipt_item_id for c in codes},
            {"SN-001": self.ri_a.id, "SN-002": self.ri_a.id, "SN-003": self.ri_b.id},
        )

    def test_registered_codes_count_as_received(self):
        self.client.post(self.url, {f"codes_{self.ri_a.id}": "SN-001"})
        code = ItemIdentityCode.objects.get(item=self.item, code="SN-001")
        self.assertTrue(code.is_received)
        self.assertIsNotNone(code.received_at)
        self.assertFalse(code.is_delivered)

    def test_unlinked_codes_are_created_without_a_receipt(self):
        response = self.client.post(self.url, {
            ItemIdentityCodesForm.UNLINKED_FIELD: "SN-H1\nSN-H2",
        })
        self.assertRedirects(response, self.url)
        codes = ItemIdentityCode.objects.filter(item=self.item).order_by("code")
        self.assertEqual([c.code for c in codes], ["SN-H1", "SN-H2"])
        self.assertTrue(all(c.receipt_item_id is None for c in codes))

    def test_unlinked_codes_count_as_in_stock_not_merely_registered(self):
        """Registering them asserts the units are physically in, so they get a
        received_at even though no receipt backs them."""
        self.client.post(self.url, {
            ItemIdentityCodesForm.UNLINKED_FIELD: "SN-H1",
        })
        code = ItemIdentityCode.objects.get(item=self.item, code="SN-H1")
        self.assertIsNone(code.receipt_item_id)
        self.assertIsNotNone(code.received_at)
        self.assertTrue(code.is_received)
        self.assertFalse(code.is_delivered)

    def test_page_works_for_an_item_with_no_purchase_orders(self):
        bare = InventoryItem.objects.create(name="Tornillo", company=self.company)
        url = reverse("admin:inventory_item_identity_codes", args=[bare.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["receipt_items"], [])

        response = self.client.post(url, {
            ItemIdentityCodesForm.UNLINKED_FIELD: "SN-OLD-1",
        })
        self.assertRedirects(response, url)
        self.assertTrue(
            ItemIdentityCode.objects.filter(item=bare, code="SN-OLD-1").exists()
        )

    def test_reposting_the_same_code_is_a_no_op(self):
        self.client.post(self.url, {f"codes_{self.ri_a.id}": "SN-001"})
        self.client.post(self.url, {f"codes_{self.ri_a.id}": "SN-001\nSN-002"})
        self.assertEqual(
            list(ItemIdentityCode.objects.filter(item=self.item)
                 .order_by("code").values_list("code", flat=True)),
            ["SN-001", "SN-002"],
        )

    def test_clash_across_lines_creates_nothing(self):
        response = self.client.post(self.url, {
            f"codes_{self.ri_a.id}": "SN-001",
            f"codes_{self.ri_b.id}": "SN-001",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ItemIdentityCode.objects.filter(item=self.item).exists())

    def test_item_from_another_company_is_not_reachable(self):
        other_company = Company.objects.create(name="Otra SL", tax_identifier="B99999999")
        other_item = InventoryItem.objects.create(name="Ajeno", company=other_company)
        staff = User.objects.create_user(
            username="staff", password="pass12345", is_staff=True,
        )
        staff.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="inventory",
                codename="add_itemidentitycode",
            )
        )
        self.client.force_login(staff)
        response = self.client.get(
            reverse("admin:inventory_item_identity_codes", args=[other_item.pk])
        )
        self.assertEqual(response.status_code, 404)


class ItemIdentityCodeInlineTest(_IdentityCodesBase):

    def _delivered_code(self, code="SN-D"):
        """A code that has shipped, so it belongs to the delivered inline."""
        client = Client.objects.create(company=self.company, name="Cliente SL")
        do = DeliveryOrder.objects.create(
            company=self.company, client=client, number="DO-001",
        )
        do_item = DeliveryOrderItem.objects.create(
            delivery_order=do, description="Perfil aluminio",
            quantity=Decimal("1"), item_number="L-1",
        )
        return ItemIdentityCode.objects.create(
            item=self.item, code=code,
            receipt_item=self.ri_a, delivery_order_item=do_item,
        ), do, client, do_item

    def test_both_inlines_are_read_only(self):
        for cls in (ItemIdentityCodeInline, DeliveredItemIdentityCodeInline):
            inline = cls(InventoryItem, admin.site)
            with self.subTest(inline=cls.__name__):
                self.assertFalse(inline.has_add_permission(None))
                self.assertFalse(inline.has_change_permission(None))
                self.assertFalse(inline.can_delete)
                self.assertEqual(inline.max_num, 0)
                # Every column must be readonly, or Django raises admin.E035.
                self.assertEqual(set(inline.fields), set(inline.readonly_fields))

    def test_the_two_inlines_get_distinct_formset_prefixes(self):
        """Both target ItemIdentityCode through the same FK, so without the
        prefix_suffix override they would share one management form."""
        stock = ItemIdentityCodeInline(InventoryItem, admin.site)
        delivered = DeliveredItemIdentityCodeInline(InventoryItem, admin.site)
        stock_prefix = stock.get_formset(None).get_default_prefix()
        delivered_prefix = delivered.get_formset(None).get_default_prefix()
        self.assertNotEqual(stock_prefix, delivered_prefix)
        self.assertIn("stock", stock_prefix)
        self.assertIn("delivered", delivered_prefix)

    def test_stock_inline_excludes_delivered_codes(self):
        in_stock = ItemIdentityCode.objects.create(
            item=self.item, code="SN-S", receipt_item=self.ri_a,
        )
        registered = ItemIdentityCode.objects.create(item=self.item, code="SN-R")
        delivered, *_ = self._delivered_code()

        inline = ItemIdentityCodeInline(InventoryItem, admin.site)
        rows = list(inline.get_queryset(self._request()))
        self.assertIn(in_stock, rows)
        self.assertIn(registered, rows)
        self.assertNotIn(delivered, rows)

    def test_delivered_inline_shows_only_delivered_codes(self):
        ItemIdentityCode.objects.create(
            item=self.item, code="SN-S", receipt_item=self.ri_a,
        )
        ItemIdentityCode.objects.create(item=self.item, code="SN-R")
        delivered, *_ = self._delivered_code()

        inline = DeliveredItemIdentityCodeInline(InventoryItem, admin.site)
        self.assertEqual(list(inline.get_queryset(self._request())), [delivered])

    def test_stock_inline_status_distinguishes_received_from_registered(self):
        inline = ItemIdentityCodeInline(InventoryItem, admin.site)
        in_stock = ItemIdentityCode.objects.create(
            item=self.item, code="SN-S", receipt_item=self.ri_a,
        )
        registered = ItemIdentityCode.objects.create(item=self.item, code="SN-R")
        # Labels go through gettext, and the project default locale is not
        # English, so pin the language rather than the rendered string.
        with translation.override("en"):
            self.assertIn("In stock", inline.status_display(in_stock))
            self.assertIn("Registered", inline.status_display(registered))

    def test_source_links_to_the_purchase_order(self):
        code = ItemIdentityCode.objects.create(
            item=self.item, code="SN-S", receipt_item=self.ri_a,
        )
        inline = ItemIdentityCodeInline(InventoryItem, admin.site)
        self.assertIn(
            reverse("admin:purchasing_purchaseorder_change", args=[self.po.pk]),
            inline.source_display(code),
        )

    def test_unreceived_code_has_no_source(self):
        code = ItemIdentityCode.objects.create(item=self.item, code="SN-R")
        inline = ItemIdentityCodeInline(InventoryItem, admin.site)
        self.assertIn("—", inline.source_display(code))

    def test_delivered_columns_carry_the_delivery_trail(self):
        code, do, client, do_item = self._delivered_code()
        inline = DeliveredItemIdentityCodeInline(InventoryItem, admin.site)

        self.assertIn(
            reverse("admin:sales_deliveryorder_change", args=[do.pk]),
            inline.delivery_order_display(code),
        )
        self.assertIn(client.name, inline.client_display(code))
        self.assertIn(do_item.item_number, inline.delivery_line_display(code))
        # Still traceable back to where it was received.
        self.assertIn(
            reverse("admin:purchasing_purchaseorder_change", args=[self.po.pk]),
            inline.source_display(code),
        )


class ItemIdentityCodeInlineRenderTest(_IdentityCodesBase):
    """Renders the real change form, so a broken template_before hook fails
    here rather than silently in the admin."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username="admin", email="a@b.c", password="pass12345",
        )
        self.client.force_login(self.user)

        self.in_stock = ItemIdentityCode.objects.create(
            item=self.item, code="SN-INSTOCK", receipt_item=self.ri_a,
        )
        self.registered = ItemIdentityCode.objects.create(
            item=self.item, code="SN-PLAIN",
        )
        sales_client = Client.objects.create(company=self.company, name="Cliente SL")
        do = DeliveryOrder.objects.create(
            company=self.company, client=sales_client, number="DO-001",
        )
        do_item = DeliveryOrderItem.objects.create(
            delivery_order=do, description="Perfil aluminio",
            quantity=Decimal("1"), item_number="L-1",
        )
        self.delivered = ItemIdentityCode.objects.create(
            item=self.item, code="SN-SHIPPED",
            receipt_item=self.ri_b, delivery_order_item=do_item,
        )
        self.url = reverse("admin:inventory_inventoryitem_change", args=[self.item.pk])

    def test_change_form_renders_both_inlines(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        self.assertTemplateUsed(response, "admin/inventory/identity_codes_actions.html")
        for code in ("SN-INSTOCK", "SN-PLAIN", "SN-SHIPPED"):
            self.assertIn(code, html)
        self.assertIn("DO-001", html)
        self.assertIn("Cliente SL", html)

    def test_template_before_comment_does_not_leak_into_the_page(self):
        """A multi-line {# #} is not a Django comment and renders verbatim."""
        response = self.client.get(self.url)
        self.assertNotContains(response, "template_before hook")

    def test_change_form_links_to_the_bulk_registration_page(self):
        response = self.client.get(self.url)
        self.assertContains(
            response,
            reverse("admin:inventory_item_identity_codes", args=[self.item.pk]),
        )

    def test_each_inline_renders_its_own_management_form(self):
        """A shared formset prefix would emit one management form for both
        inlines and break saving the item. Assert they are separate."""
        response = self.client.get(self.url)
        html = response.content.decode()
        self.assertIn("identity_codes-stock-TOTAL_FORMS", html)
        self.assertIn("identity_codes-delivered-TOTAL_FORMS", html)

        prefixes = [fs.formset.prefix for fs in response.context["inline_admin_formsets"]]
        self.assertEqual(len(prefixes), len(set(prefixes)))

    def test_add_form_renders_without_an_object(self):
        """identity_codes_actions.html reverses a URL from original.pk, which
        is absent on the add form."""
        response = self.client.get(reverse("admin:inventory_inventoryitem_add"))
        self.assertEqual(response.status_code, 200)
