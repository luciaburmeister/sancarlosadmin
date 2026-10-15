"""
The purchase order changelist keeps the main table narrow and pushes the
secondary fields (type, cost center, requester, payment, approval trail)
into an expandable Unfold section under each row.

Run just this file:
    python manage.py test purchasing.tests.test_po_changelist_section
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone, translation

from accounting.models import CostCenter
from purchasing.admin import PurchaseOrderAdmin, PurchaseOrderDetailsSection
from purchasing.models import PurchaseOrderStatus, PurchaseOrderType
from purchasing.tests.factories import make_company, make_provider, make_purchase_order


class PurchaseOrderChangelistSectionTest(TestCase):
    def setUp(self):
        self.company = make_company()
        self.cost_center = CostCenter.objects.create(
            company=self.company, code="211", name="Flota Cuba"
        )
        # A new PO snapshots payment terms from its provider, so set them there.
        self.provider = make_provider(
            company=self.company, default_cost_center=self.cost_center,
            payment_terms_days=30,
        )
        self.user = get_user_model().objects.create_superuser(
            username="admin", email="admin@example.com", password="pw",
            first_name="Ana", last_name="Compras",
        )
        self.po = make_purchase_order(
            company=self.company, provider=self.provider,
            date_ordered=date(2026, 6, 1), po_type=PurchaseOrderType.STOCK,
            requested_by=self.user, status=PurchaseOrderStatus.APPROVED,
            approved_by=self.user, approved_at=timezone.now(),
        )
        self.client.force_login(self.user)

    def _render(self, po):
        request = RequestFactory().get("/")
        request.user = self.user
        # Labels are asserted by their English msgids, whatever the active locale.
        with translation.override("en"):
            return PurchaseOrderDetailsSection(request, po).render()

    def test_secondary_columns_moved_out_of_list_display(self):
        for column in ("po_type", "cost_center", "requested_by"):
            self.assertNotIn(column, PurchaseOrderAdmin.list_display)
        self.assertIn(PurchaseOrderDetailsSection, PurchaseOrderAdmin.list_sections)

    def test_changelist_renders_with_the_section(self):
        response = self.client.get(reverse("admin:purchasing_purchaseorder_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 1)

    def test_section_shows_type_cost_center_requester_and_approval(self):
        html = self._render(self.po)

        with translation.override("en"):
            self.assertIn(self.po.get_po_type_display(), html)
            self.assertIn(self.po.get_payment_method_display(), html)
            # i18n_patterns prefix the URL with the active language.
            self.assertIn(
                reverse("admin:accounting_costcenter_change", args=[self.cost_center.pk]), html
            )
        self.assertIn(str(self.cost_center), html)
        self.assertIn("Ana Compras", html)
        self.assertIn(">30<", html)
        self.assertIn("Approved by", html)
        self.assertNotIn("Rejected by", html)

    def test_section_shows_rejection_trail_when_rejected(self):
        self.po.status = PurchaseOrderStatus.REJECTED
        self.po.rejected_by = self.user
        self.po.rejected_at = timezone.now()
        self.po.rejection_reason = "Presupuesto agotado"
        self.po.save()

        html = self._render(self.po)

        self.assertIn("Rejected by", html)
        self.assertIn("Presupuesto agotado", html)
        self.assertNotIn("Approved by", html)

    def test_section_handles_empty_relations(self):
        bare = make_purchase_order(
            company=self.company,
            provider=make_provider(company=self.company, name="Sin centro"),
            date_ordered=date(2026, 6, 2),
        )
        html = self._render(bare)
        self.assertIn("Classification", html)
