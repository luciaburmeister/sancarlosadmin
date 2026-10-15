"""
Tests for the permission-gated line total override on QuoteInventoryItem.

Covers:
- Setting override_line_total pins line_total and derives the discount %
- Clearing the override falls back to unit_price * quantity * discount
- Quote totals (subtotal / tax / total) follow the overridden line
- Line-item discount policies leave overridden lines alone
- Validation: negative overrides and out-of-range derived discounts
- Admin gating: the field is read-only without sales.can_override_line_total
"""
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from djmoney.money import Money

from inventory.models import InventoryItem
from invoices.models import TaxType
from sales.admin.quote import QuoteInventoryItemAdmin, QuoteInventoryItemInline
from sales.models import DiscountLevel, Quote, QuoteInventoryItem
from sales.tests.factories import (
    apply_taxes,
    make_client,
    make_company,
    make_discount_policy,
    make_inventory_item,
    make_quote,
    make_quote_inventory_line,
)


class _OverrideBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Override Test Co")
        cls.client_obj = make_client(company=cls.company, name="Override Client")

    def _make_quote(self):
        return make_quote(company=self.company, client=self.client_obj, project_name="Override Test")

    def _make_line(self, quote, unit_price, quantity, discount=Decimal("0"), override=None):
        item = make_inventory_item(
            company=self.company, name="Override Item",
            code=f"OVR-{InventoryItem.objects.count() + 1}", selling_price=Money(unit_price, "EUR"),
        )
        return make_quote_inventory_line(
            quote=quote, item=item, quantity=Decimal(str(quantity)),
            unit_price=Money(unit_price, "EUR"), discount=discount,
            override_line_total=Money(override, "EUR") if override is not None else None,
        )


class TestOverrideLineTotalMath(_OverrideBase):
    def test_no_override_uses_discount_formula(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 2, discount=Decimal("10"))
        self.assertFalse(line.has_override)
        self.assertEqual(line.line_total.amount, Decimal("180.00"))

    def test_override_pins_line_total_and_derives_discount(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 2, override="150")
        line.refresh_from_db()
        self.assertTrue(line.has_override)
        self.assertEqual(line.line_total.amount, Decimal("150.00"))
        self.assertEqual(line.discount, Decimal("25.00"))

    def test_override_ignores_entered_discount(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 1, discount=Decimal("50"), override="90")
        line.refresh_from_db()
        self.assertEqual(line.line_total.amount, Decimal("90.00"))
        self.assertEqual(line.discount, Decimal("10.00"))

    def test_override_keeps_exact_amount_when_pct_rounds(self):
        # 333.33 * 3 = 999.99; 900 implies 9.999...% -> stored as 10.00%,
        # but the line total must stay exactly what was typed.
        quote = self._make_quote()
        line = self._make_line(quote, "333.33", 3, override="900")
        line.refresh_from_db()
        self.assertEqual(line.line_total.amount, Decimal("900.00"))
        self.assertEqual(line.discount, Decimal("10.00"))

    def test_override_above_gross_yields_negative_discount(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 1, override="120")
        line.refresh_from_db()
        self.assertEqual(line.line_total.amount, Decimal("120.00"))
        self.assertEqual(line.discount, Decimal("-20.00"))

    def test_override_with_zero_unit_price(self):
        quote = self._make_quote()
        inv = make_inventory_item(company=self.company, name="Free Item", code="OVR-ZERO")
        line = make_quote_inventory_line(
            quote=quote, item=inv, quantity=Decimal("1"), override_line_total=Money("40", "EUR"),
        )
        line.refresh_from_db()
        self.assertEqual(line.line_total.amount, Decimal("40.00"))
        self.assertEqual(line.discount, Decimal("0.00"))

    def test_clearing_override_restores_formula(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 2, override="150")
        line.override_line_total = None
        line.discount = Decimal("0")
        line.save()
        line.refresh_from_db()
        self.assertFalse(line.has_override)
        self.assertEqual(line.line_total.amount, Decimal("200.00"))

    def test_changing_quantity_keeps_override_and_rederives_discount(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 2, override="150")
        line.quantity = Decimal("4")
        line.save()
        line.refresh_from_db()
        self.assertEqual(line.line_total.amount, Decimal("150.00"))
        self.assertEqual(line.discount, Decimal("62.50"))


class TestOverrideQuoteTotals(_OverrideBase):
    def test_quote_subtotal_and_tax_follow_override(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 2, override="150")
        iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES",
            defaults={"behavior": "add", "percentage_options": [0, 10, 21], "default_percentage": 21},
        )
        apply_taxes(line, taxes=[(iva, Decimal("21"))])
        line.save()
        line.refresh_from_db()
        quote.refresh_from_db()

        self.assertEqual(line.total_with_tax.amount, Decimal("181.50"))
        self.assertEqual(quote.subtotal_price.amount, Decimal("150.00"))
        self.assertEqual(quote.total_price.amount, Decimal("181.50"))

    def test_mixed_lines(self):
        quote = self._make_quote()
        self._make_line(quote, "100", 1, discount=Decimal("10"))   # 90
        self._make_line(quote, "100", 1, override="75")            # 75
        quote.refresh_from_db()
        self.assertEqual(quote.subtotal_price.amount, Decimal("165.00"))


class TestOverrideVsDiscountPolicies(_OverrideBase):
    def test_line_item_policy_skips_overridden_lines(self):
        quote = self._make_quote()
        plain = self._make_line(quote, "100", 1)
        pinned = self._make_line(quote, "100", 1, override="80")

        policy = make_discount_policy(company=self.company, name="Line 5%", rules=[
            {"name": "5% off", "percentage": Decimal("5"), "level": DiscountLevel.LINE_ITEM},
        ])
        quote.discount_policies.add(policy)
        quote.recalc_totals()

        plain.refresh_from_db()
        pinned.refresh_from_db()
        quote.refresh_from_db()

        self.assertEqual(plain.discount, Decimal("5.00"))
        self.assertEqual(plain.line_total.amount, Decimal("95.00"))
        self.assertEqual(pinned.discount, Decimal("20.00"))
        self.assertEqual(pinned.line_total.amount, Decimal("80.00"))
        self.assertEqual(quote.subtotal_price.amount, Decimal("175.00"))


class TestOverrideValidation(_OverrideBase):
    def test_negative_override_rejected(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 1)
        line.override_line_total = Money("-1", "EUR")
        with self.assertRaises(ValidationError) as ctx:
            line.full_clean()
        self.assertIn("override_line_total", ctx.exception.message_dict)

    def test_override_implying_out_of_range_discount_rejected(self):
        quote = self._make_quote()
        line = self._make_line(quote, "1", 1)
        line.override_line_total = Money("5000", "EUR")  # -499900 %
        with self.assertRaises(ValidationError) as ctx:
            line.full_clean()
        self.assertIn("override_line_total", ctx.exception.message_dict)

    def test_valid_override_passes_clean(self):
        quote = self._make_quote()
        line = self._make_line(quote, "100", 1)
        line.override_line_total = Money("80", "EUR")
        line.full_clean()  # must not raise


class TestOverrideAdminGating(_OverrideBase):
    def setUp(self):
        User = get_user_model()
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.plain_user = User.objects.create_user(
            username="plain", password="x", is_staff=True,
        )
        self.priv_user = User.objects.create_user(
            username="priv", password="x", is_staff=True,
        )
        perm = Permission.objects.get(
            codename="can_override_line_total",
            content_type=ContentType.objects.get_for_model(QuoteInventoryItem),
        )
        self.priv_user.user_permissions.add(perm)
        self.assertTrue(
            get_user_model().objects.get(pk=self.priv_user.pk).has_perm(
                "sales.can_override_line_total"
            )
        )

    def _request(self, user):
        request = self.factory.get("/admin/sales/quote/1/change/")
        request.user = user
        return request

    def test_inline_readonly_without_permission(self):
        inline = QuoteInventoryItemInline(Quote, self.site)
        readonly = inline.get_readonly_fields(self._request(self.plain_user))
        self.assertIn("override_line_total", readonly)
        self.assertIn("override_line_total", inline.fields)

    def test_inline_editable_with_permission(self):
        inline = QuoteInventoryItemInline(Quote, self.site)
        readonly = inline.get_readonly_fields(self._request(self.priv_user))
        self.assertNotIn("override_line_total", readonly)
        # Existing read-only columns are untouched.
        self.assertIn("unit_price", readonly)
        self.assertIn("line_total", readonly)

    def test_standalone_admin_gated_too(self):
        model_admin = QuoteInventoryItemAdmin(QuoteInventoryItem, self.site)
        self.assertIn(
            "override_line_total",
            model_admin.get_readonly_fields(self._request(self.plain_user)),
        )
        self.assertNotIn(
            "override_line_total",
            model_admin.get_readonly_fields(self._request(self.priv_user)),
        )
