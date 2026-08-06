"""
purchasing/tests/test_po_type.py

PurchaseOrder.po_type: project purchases require a project file; stock
material orders don't and never keep one.

Run:
    python manage.py test purchasing.tests.test_po_type
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase

from company.models import Company
from management.models import ProjectFile
from materials.models import Provider
from purchasing.models import PurchaseOrder, PurchaseOrderType
from sales.models import Client


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
