from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from inventory.models import InventoryItem, Warehouse
from invoices.models import Invoice, InvoiceItem, InvoicePOAllocation
from materials.models import Provider
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptItem,
    PurchaseOrderStatus,
)


class InvoicePOAllocationTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", tax_identifier="B12345678")
        self.provider = Provider.objects.create(company=self.company, name="Proveedor SA")
        self.inventory_item = InventoryItem.objects.create(name="Perfil aluminio", company=self.company)
        self.warehouse = Warehouse.objects.create(company=self.company, code="WH1", name="Central")

        self.po = PurchaseOrder.objects.create(
            company=self.company,
            provider=self.provider,
            date_ordered=date(2026, 6, 1),
            status=PurchaseOrderStatus.APPROVED,
        )
        self.po_item = PurchaseOrderItem.objects.create(
            purchase_order=self.po,
            inventory_item=self.inventory_item,
            quantity=Decimal("10"),
            unit_price=Money("5.00", "EUR"),
        )

        self.receipt = PurchaseOrderReceipt.objects.create(
            company=self.company,
            purchase_order=self.po,
            warehouse=self.warehouse,
        )
        self.receipt_item = PurchaseOrderReceiptItem.objects.create(
            receipt=self.receipt,
            po_item=self.po_item,
            quantity_received=Decimal("10"),
        )

        self.invoice = self._make_invoice("F-001")
        self.invoice_item = self._make_invoice_item(self.invoice, "Perfil aluminio", "10")

    def _make_invoice(self, number, provider=None, company=None):
        return Invoice.objects.create(
            company=company or self.company,
            provider=provider or self.provider,
            invoice_number=number,
            date_issued=date(2026, 6, 20),
        )

    def _make_invoice_item(self, invoice, description, quantity):
        return InvoiceItem.objects.create(
            invoice=invoice,
            description=description,
            quantity=Decimal(quantity),
            unit_price=Money("5.00", "EUR"),
        )

    def _allocate(self, quantity, invoice_item=None, po_item=None):
        return InvoicePOAllocation.objects.create(
            invoice_item=invoice_item or self.invoice_item,
            po_item=po_item or self.po_item,
            quantity=Decimal(quantity),
        )

    def _invoiced(self):
        self.po_item.refresh_from_db()
        return self.po_item.invoiced_quantity

    def test_create_allocation_updates_invoiced_quantity(self):
        self._allocate("4")
        self.assertEqual(self._invoiced(), Decimal("4"))
        self.assertEqual(self.po_item.pending_invoice_quantity, Decimal("6"))

    def test_invoice_fk_autoset_from_invoice_item(self):
        alloc = self._allocate("4")
        self.assertEqual(alloc.invoice, self.invoice)

    def test_update_allocation_recalculates(self):
        alloc = self._allocate("4")
        alloc.quantity = Decimal("6")
        alloc.save()
        self.assertEqual(self._invoiced(), Decimal("6"))

    def test_delete_allocation_recalculates(self):
        alloc = self._allocate("4")
        alloc.delete()
        self.assertEqual(self._invoiced(), Decimal("0"))

    def test_cascade_delete_invoice_resyncs(self):
        self._allocate("4")
        self.invoice.delete()
        self.assertEqual(self._invoiced(), Decimal("0"))

    def test_allocations_from_two_invoices_sum(self):
        self._allocate("4")
        other_invoice = self._make_invoice("F-002")
        other_item = self._make_invoice_item(other_invoice, "Perfil aluminio", "6")
        self._allocate("6", invoice_item=other_item)
        self.assertEqual(self._invoiced(), Decimal("10"))

    def test_over_allocation_blocked_on_create(self):
        self._allocate("4")
        other_invoice = self._make_invoice("F-002")
        other_item = self._make_invoice_item(other_invoice, "Perfil aluminio", "7")
        with self.assertRaises(ValidationError):
            self._allocate("7", invoice_item=other_item)
        self.assertEqual(self._invoiced(), Decimal("4"))

    def test_over_allocation_blocked_on_update(self):
        alloc = self._allocate("4")
        alloc.quantity = Decimal("11")
        with self.assertRaises(ValidationError):
            alloc.save()
        self.assertEqual(self._invoiced(), Decimal("4"))

    def test_zero_or_negative_quantity_blocked(self):
        with self.assertRaises(ValidationError):
            self._allocate("0")
        with self.assertRaises(ValidationError):
            self._allocate("-1")

    def test_provider_mismatch_blocked(self):
        other_provider = Provider.objects.create(company=self.company, name="Otro Proveedor")
        invoice = self._make_invoice("F-003", provider=other_provider)
        item = self._make_invoice_item(invoice, "Perfil aluminio", "2")
        with self.assertRaises(ValidationError):
            self._allocate("2", invoice_item=item)

    def test_null_provider_allowed(self):
        invoice = Invoice.objects.create(
            company=self.company,
            invoice_number="F-004",
            date_issued=date(2026, 6, 21),
        )
        item = self._make_invoice_item(invoice, "Perfil aluminio", "2")
        self._allocate("2", invoice_item=item)
        self.assertEqual(self._invoiced(), Decimal("2"))

    def test_company_mismatch_blocked(self):
        other_company = Company.objects.create(name="Other Co", tax_identifier="B87654321")
        invoice = self._make_invoice("F-005", company=other_company)
        item = self._make_invoice_item(invoice, "Perfil aluminio", "2")
        with self.assertRaises(ValidationError):
            self._allocate("2", invoice_item=item)

    def test_draft_and_cancelled_po_blocked(self):
        for status in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.CANCELLED):
            po = PurchaseOrder.objects.create(
                company=self.company,
                provider=self.provider,
                date_ordered=date(2026, 6, 1),
                status=status,
            )
            po_item = PurchaseOrderItem.objects.create(
                purchase_order=po,
                inventory_item=self.inventory_item,
                quantity=Decimal("5"),
                unit_price=Money("5.00", "EUR"),
            )
            with self.assertRaises(ValidationError):
                self._allocate("2", po_item=po_item)

    def test_closed_po_allowed(self):
        # Fully-received POs auto-close; they are exactly the ones being invoiced.
        self.po.status = PurchaseOrderStatus.CLOSED
        self.po.save(update_fields=["status"])
        self._allocate("4")
        self.assertEqual(self._invoiced(), Decimal("4"))

    def test_linked_purchase_orders_derived(self):
        other_po = PurchaseOrder.objects.create(
            company=self.company,
            provider=self.provider,
            date_ordered=date(2026, 6, 2),
            status=PurchaseOrderStatus.APPROVED,
        )
        other_po_item = PurchaseOrderItem.objects.create(
            purchase_order=other_po,
            inventory_item=self.inventory_item,
            quantity=Decimal("5"),
            unit_price=Money("5.00", "EUR"),
        )
        self._allocate("4")
        second_item = self._make_invoice_item(self.invoice, "Otra línea", "2")
        self._allocate("2", invoice_item=second_item, po_item=other_po_item)

        linked = list(self.invoice.linked_purchase_orders().order_by("id"))
        self.assertEqual(linked, [self.po, other_po])

    def test_billing_status_transitions(self):
        self.assertEqual(self.po.billing_status, "not_invoiced")
        alloc = self._allocate("4")
        self.assertEqual(self.po.billing_status, "partially_invoiced")
        alloc.quantity = Decimal("10")
        alloc.save()
        self.assertEqual(self.po.billing_status, "fully_invoiced")

    def test_allocated_amount_property(self):
        alloc = self._allocate("4")
        self.assertEqual(alloc.allocated_amount, Money("20.00", "EUR"))
