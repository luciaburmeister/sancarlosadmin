"""
A purchase order picks up its provider's cost center at creation.

Run just this file:
    python manage.py test purchasing.tests.test_cost_center_propagation
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from accounting.models import CostCenter
from purchasing.models import PurchaseOrder, PurchaseOrderStatus
from purchasing.tests.factories import make_company, make_provider, make_purchase_order


class PurchaseOrderCostCenterTest(TestCase):
    def setUp(self):
        self.company = make_company()
        self.flota = CostCenter.objects.create(
            company=self.company, code="210", name="Flota", is_assignable=False
        )
        self.cuba = CostCenter.objects.create(
            company=self.company, code="211", name="Flota Cuba", parent=self.flota
        )
        self.taller = CostCenter.objects.create(
            company=self.company, code="220", name="Taller"
        )
        self.provider = make_provider(company=self.company, default_cost_center=self.cuba)

    def _make_po(self, provider=None, **kwargs):
        return make_purchase_order(
            company=self.company, provider=provider or self.provider,
            date_ordered=date(2026, 6, 1), **kwargs,
        )

    # ---- Defaulting at creation ----

    def test_provider_default_is_stamped_on_a_new_po(self):
        po = self._make_po()
        self.assertEqual(po.cost_center_id, self.cuba.pk)

    def test_explicit_cost_center_is_not_overridden(self):
        po = self._make_po(cost_center=self.taller)
        self.assertEqual(po.cost_center_id, self.taller.pk)

    def test_provider_without_a_default_leaves_it_empty(self):
        other = make_provider(company=self.company, name="Sin centro")
        po = self._make_po(provider=other)
        self.assertIsNone(po.cost_center_id)

    def test_inactive_default_is_not_copied(self):
        self.cuba.is_active = False
        self.cuba.save()
        po = self._make_po()
        self.assertIsNone(po.cost_center_id)

    def test_grouping_only_default_is_not_copied(self):
        self.provider.default_cost_center = self.flota  # is_assignable=False
        self.provider.save()
        po = self._make_po()
        self.assertIsNone(po.cost_center_id)

    # ---- Existing orders ----

    def test_clearing_on_an_existing_po_sticks(self):
        po = self._make_po()
        self.assertEqual(po.cost_center_id, self.cuba.pk)
        po.cost_center = None
        po.save()
        po.refresh_from_db()
        self.assertIsNone(po.cost_center_id)

    def test_changing_the_provider_default_does_not_touch_existing_pos(self):
        po = self._make_po()
        self.provider.default_cost_center = self.taller
        self.provider.save()
        po.save()
        po.refresh_from_db()
        self.assertEqual(po.cost_center_id, self.cuba.pk)

    # ---- Validation ----

    def test_cost_center_from_another_company_is_rejected(self):
        other_company = make_company(name="Otra SA", tax_identifier="B87654321")
        foreign = CostCenter.objects.create(company=other_company, code="900", name="Ajeno")
        po = self._make_po(save=False, cost_center=foreign)
        with self.assertRaises(ValidationError) as ctx:
            po.clean()
        self.assertIn("cost_center", ctx.exception.message_dict)

    def test_grouping_only_cost_center_is_rejected(self):
        po = self._make_po(save=False, cost_center=self.flota)
        with self.assertRaises(ValidationError) as ctx:
            po.clean()
        self.assertIn("cost_center", ctx.exception.message_dict)

    # ---- Admin: correctable after approval ----

    def test_cost_center_stays_editable_once_the_po_leaves_draft(self):
        from purchasing.admin import PurchaseOrderAdmin
        from django.contrib.admin.sites import site

        po = self._make_po(status=PurchaseOrderStatus.APPROVED)
        model_admin = PurchaseOrderAdmin(PurchaseOrder, site)
        readonly = model_admin.get_readonly_fields(request=None, obj=po)
        self.assertNotIn("cost_center", readonly)
        # Everything else is still locked.
        self.assertIn("provider", readonly)


class CostCenterAdminSmokeTest(TestCase):
    """The admin pages the new field touches must actually render."""

    def setUp(self):
        self.company = make_company()
        self.cuba = CostCenter.objects.create(
            company=self.company, code="211", name="Flota Cuba"
        )
        self.provider = make_provider(company=self.company, default_cost_center=self.cuba)
        self.po = make_purchase_order(
            company=self.company, provider=self.provider,
            date_ordered=date(2026, 6, 1), status=PurchaseOrderStatus.APPROVED,
        )
        user = get_user_model().objects.create_superuser(
            username="admin", email="admin@example.com", password="pw"
        )
        self.client.force_login(user)

    def test_cost_center_changelist_and_add_form_render(self):
        for url in (
            reverse("admin:accounting_costcenter_changelist"),
            reverse("admin:accounting_costcenter_add"),
            reverse("admin:accounting_costcenter_change", args=[self.cuba.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_approved_po_change_form_renders_with_editable_cost_center(self):
        url = reverse("admin:purchasing_purchaseorder_change", args=[self.po.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("cost_center", response.context["adminform"].form.fields)

    def test_changelists_accept_the_cost_center_filter(self):
        for name in (
            "admin:purchasing_purchaseorder_changelist",
            "admin:invoices_invoice_changelist",
            "admin:materials_provider_changelist",
        ):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)

        response = self.client.get(
            reverse("admin:purchasing_purchaseorder_changelist"),
            {"cost_center__id__exact": self.cuba.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 1)

    def test_export_carries_the_cost_center_path(self):
        from purchasing.resources import PurchaseOrderResource

        estructura = CostCenter.objects.create(
            company=self.company, code="200", name="Operaciones", is_assignable=False
        )
        self.cuba.parent = estructura
        self.cuba.save()

        dataset = PurchaseOrderResource().export(PurchaseOrder.objects.all())
        headers = list(dataset.headers)
        row = dict(zip(headers, dataset[0]))
        self.assertEqual(row[str(_("Cost Center Code"))], "211")
        self.assertEqual(row[str(_("Cost Center"))], "Operaciones / Flota Cuba")
