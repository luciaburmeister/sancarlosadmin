"""
purchasing/tests/test_po_type.py

PurchaseOrder.po_type: project purchases require a project file; stock
material orders don't and never keep one.

Run:
    python manage.py test purchasing.tests.test_po_type
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from company.models import Company
from management.models import ProjectFile
from materials.models import Provider
from purchasing.models import PurchaseOrder, PurchaseOrderStatus, PurchaseOrderType
from sales.models import Client

User = get_user_model()


class POTypeTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", tax_identifier="B12345678")
        self.provider = Provider.objects.create(company=self.company, name="Proveedor SA")
        client = Client.objects.create(name="Client 1", company=self.company)
        self.project_file = ProjectFile.objects.create(name="Project 1", client=client, company=self.company)

    def _po(self, **kwargs):
        return PurchaseOrder(
            company=self.company,
            provider=self.provider,
            date_ordered=date(2026, 8, 1),
            **kwargs,
        )

    def test_project_po_without_project_file_fails_validation(self):
        po = self._po(po_type=PurchaseOrderType.PROJECT)
        with self.assertRaises(ValidationError) as ctx:
            po.full_clean()
        self.assertIn("project_file", ctx.exception.message_dict)

    def test_project_po_with_project_file_validates(self):
        po = self._po(po_type=PurchaseOrderType.PROJECT, project_file=self.project_file)
        po.full_clean()

    def test_stock_po_without_project_file_validates(self):
        po = self._po(po_type=PurchaseOrderType.STOCK)
        po.full_clean()

    def test_stock_po_clears_project_file_on_save(self):
        po = self._po(po_type=PurchaseOrderType.STOCK, project_file=self.project_file)
        po.save()
        po.refresh_from_db()
        self.assertIsNone(po.project_file)

    def test_default_type_is_project(self):
        self.assertEqual(self._po().po_type, PurchaseOrderType.PROJECT)


class POChangePageProjectFileTests(POTypeTests):
    """The project file must stay visible on locked (non-draft) project POs,
    where every field is readonly and Alpine has no po_type input to read."""

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser(username="admin", password="x", email="a@a.com")
        self.client.force_login(self.superuser)

    def _change_page(self, po):
        return self.client.get(reverse("admin:purchasing_purchaseorder_change", args=[po.pk]))

    def test_locked_project_po_shows_project_file(self):
        po = self._po(
            po_type=PurchaseOrderType.PROJECT,
            project_file=self.project_file,
            status=PurchaseOrderStatus.APPROVED,
        )
        po.save()
        response = self._change_page(po)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Project 1", content)
        self.assertIn("typeof po_type === &#x27;undefined&#x27;", content)

    def test_locked_stock_po_hides_project_file_row(self):
        po = self._po(po_type=PurchaseOrderType.STOCK, status=PurchaseOrderStatus.APPROVED)
        po.save()
        response = self._change_page(po)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('class="field-project_file"', response.content.decode())

    def test_draft_po_keeps_conditional_project_file_field(self):
        po = self._po(po_type=PurchaseOrderType.PROJECT, project_file=self.project_file)
        po.save()
        response = self._change_page(po)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('name="project_file"', content)
        self.assertIn("x-model.fill", content)
