"""
Cost center on supplier invoices: the provider default at creation, and the
matched purchase order taking precedence over it.

Run just this file:
    python manage.py test invoices.tests.test_cost_center_propagation
"""

from decimal import Decimal

from accounting.models import CostCenter
from invoices.services.po_matching import POInvoiceMatchService
from invoices.tests.test_po_matching import POMatchingTestBase


class InvoiceCostCenterTest(POMatchingTestBase):
    def setUp(self):
        super().setUp()
        self.cuba = CostCenter.objects.create(
            company=self.company, code="211", name="Flota Cuba"
        )
        self.taller = CostCenter.objects.create(
            company=self.company, code="220", name="Taller"
        )
        self.admin = CostCenter.objects.create(
            company=self.company, code="110", name="Administracion"
        )

    def _confirm_profile_line(self, quantity="10"):
        return POInvoiceMatchService.confirm([{
            "invoice_item": self.inv_line_profile,
            "po_item": self.po_line_profile,
            "quantity": Decimal(quantity),
        }])

    # ---- Provider default at creation ----

    def test_provider_default_is_stamped_on_a_new_invoice(self):
        self.provider.default_cost_center = self.taller
        self.provider.save()
        invoice = self._make_invoice("F-100")
        self.assertEqual(invoice.cost_center_id, self.taller.pk)

    def test_no_provider_default_leaves_it_empty(self):
        invoice = self._make_invoice("F-101")
        self.assertIsNone(invoice.cost_center_id)

    # ---- Matching a purchase order ----

    def test_matched_po_fills_an_empty_cost_center(self):
        self.po.cost_center = self.cuba
        self.po.save()

        self._confirm_profile_line()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.cost_center_id, self.cuba.pk)

    def test_matched_po_overrides_an_untouched_provider_default(self):
        self.provider.default_cost_center = self.taller
        self.provider.save()
        self.invoice.cost_center = self.taller  # as if inherited at creation
        self.invoice.save()
        self.po.cost_center = self.cuba
        self.po.save()

        self._confirm_profile_line()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.cost_center_id, self.cuba.pk)

    def test_matched_po_never_overrides_a_hand_set_cost_center(self):
        self.provider.default_cost_center = self.taller
        self.provider.save()
        self.invoice.cost_center = self.admin  # deliberately not the default
        self.invoice.save()
        self.po.cost_center = self.cuba
        self.po.save()

        self._confirm_profile_line()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.cost_center_id, self.admin.pk)

    def test_po_without_a_cost_center_leaves_the_invoice_alone(self):
        self.invoice.cost_center = self.admin
        self.invoice.save()

        self._confirm_profile_line()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.cost_center_id, self.admin.pk)

    def test_project_file_propagation_still_works_alongside(self):
        from management.models import ProjectFile
        from sales.models import Client

        client = Client.objects.create(company=self.company, name="Cliente SA")
        project = ProjectFile.objects.create(
            company=self.company, name="Obra 1", client=client
        )
        self.po.project_file = project
        self.po.cost_center = self.cuba
        self.po.save()

        self._confirm_profile_line()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.project_file_id, project.pk)
        self.assertEqual(self.invoice.cost_center_id, self.cuba.pk)
