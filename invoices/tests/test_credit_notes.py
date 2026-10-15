from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from inventory.models import InventoryItem
from invoices.models import (
    Invoice,
    InvoiceDocumentType,
    InvoiceItem,
    InvoicePaymentStatus,
    InvoicePOAllocation,
)
from invoices.services.po_matching import POInvoiceMatchService
from materials.models import Provider
from purchasing.models import PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus


class CreditNoteTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", tax_identifier="B12345678")
        self.provider = Provider.objects.create(company=self.company, name="Proveedor SA")
        self.inventory_item = InventoryItem.objects.create(name="Perfil aluminio", company=self.company)

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

        self.invoice = self._make_invoice("F-001")
        self.invoice_item = self._make_invoice_item(self.invoice, "Perfil aluminio", "10", "5.00")

    def _make_invoice(self, number, document_type=InvoiceDocumentType.INVOICE):
        return Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number=number,
            date_issued=date(2026, 6, 20),
            document_type=document_type,
        )

    def _make_credit_note(self, number, rectifies=None):
        credit_note = self._make_invoice(number, document_type=InvoiceDocumentType.CREDIT_NOTE)
        if rectifies:
            credit_note.rectifies = rectifies
            credit_note.save(update_fields=["rectifies"])
        return credit_note

    def _make_invoice_item(self, invoice, description, quantity, price):
        return InvoiceItem.objects.create(
            invoice=invoice,
            description=description,
            quantity=Decimal(quantity),
            unit_price=Money(price, "EUR"),
        )

    def _allocate(self, invoice_item, quantity):
        return InvoicePOAllocation.objects.create(
            invoice_item=invoice_item,
            po_item=self.po_item,
            quantity=Decimal(quantity),
        )

    def _invoiced(self):
        self.po_item.refresh_from_db()
        return self.po_item.invoiced_quantity


class CreditNoteAllocationTest(CreditNoteTestBase):
    def test_credit_allocation_subtracts_from_invoiced_quantity(self):
        self._allocate(self.invoice_item, "6")
        credit_note = self._make_credit_note("A-001", rectifies=self.invoice)
        credit_item = self._make_invoice_item(credit_note, "Perfil aluminio", "1", "-10.00")
        self._allocate(credit_item, "2")
        self.assertEqual(self._invoiced(), Decimal("4"))
        self.assertEqual(self.po_item.pending_invoice_quantity, Decimal("6"))

    def test_over_crediting_blocked(self):
        self._allocate(self.invoice_item, "4")
        credit_note = self._make_credit_note("A-001")
        credit_item = self._make_invoice_item(credit_note, "Perfil aluminio", "1", "-10.00")
        with self.assertRaises(ValidationError):
            self._allocate(credit_item, "5")
        self.assertEqual(self._invoiced(), Decimal("4"))

    def test_crediting_uninvoiced_po_line_blocked(self):
        credit_note = self._make_credit_note("A-001")
        credit_item = self._make_invoice_item(credit_note, "Perfil aluminio", "1", "-10.00")
        with self.assertRaises(ValidationError):
            self._allocate(credit_item, "1")

    def test_reinvoicing_reopened_quantity_allowed(self):
        self._allocate(self.invoice_item, "10")
        credit_note = self._make_credit_note("A-001", rectifies=self.invoice)
        credit_item = self._make_invoice_item(credit_note, "Perfil aluminio", "3", "-5.00")
        self._allocate(credit_item, "3")
        self.assertEqual(self._invoiced(), Decimal("7"))

        second_invoice = self._make_invoice("F-002")
        second_item = self._make_invoice_item(second_invoice, "Perfil aluminio", "3", "5.00")
        self._allocate(second_item, "3")
        self.assertEqual(self._invoiced(), Decimal("10"))

    def test_deleting_credit_allocation_restores_invoiced_quantity(self):
        self._allocate(self.invoice_item, "6")
        credit_note = self._make_credit_note("A-001")
        credit_item = self._make_invoice_item(credit_note, "Perfil aluminio", "1", "-10.00")
        alloc = self._allocate(credit_item, "2")
        self.assertEqual(self._invoiced(), Decimal("4"))
        alloc.delete()
        self.assertEqual(self._invoiced(), Decimal("6"))


class CreditNoteMatchingTest(CreditNoteTestBase):
    def test_credit_note_candidates_require_invoiced_quantity(self):
        credit_note = self._make_credit_note("A-001")
        self._make_invoice_item(credit_note, "Perfil aluminio", "1", "-10.00")

        # Nothing invoiced yet: no creditable PO.
        self.assertEqual(POInvoiceMatchService.candidate_pos_for_invoice(credit_note), [])

        self._allocate(self.invoice_item, "6")
        candidates = POInvoiceMatchService.candidate_pos_for_invoice(credit_note)
        self.assertEqual([po.pk for po in candidates], [self.po.pk])
        self.assertEqual(candidates[0].pending_to_invoice, Decimal("6"))

    def test_regular_invoice_candidates_unchanged(self):
        candidates = POInvoiceMatchService.candidate_pos_for_invoice(self.invoice)
        self.assertEqual([po.pk for po in candidates], [self.po.pk])
        self.assertEqual(candidates[0].pending_to_invoice, Decimal("10"))

    def test_score_matches_negative_price_against_positive(self):
        score_negative = POInvoiceMatchService._score(
            "Perfil aluminio", Money("-5.00", "EUR"), "Perfil aluminio", Money("5.00", "EUR")
        )
        score_positive = POInvoiceMatchService._score(
            "Perfil aluminio", Money("5.00", "EUR"), "Perfil aluminio", Money("5.00", "EUR")
        )
        self.assertEqual(score_negative, score_positive)

    def test_suggestions_for_credit_note_capped_by_invoiced_quantity(self):
        self._allocate(self.invoice_item, "2")
        credit_note = self._make_credit_note("A-001")
        credit_item = self._make_invoice_item(credit_note, "Perfil aluminio", "5", "-5.00")

        suggestions = POInvoiceMatchService.suggest_pairs_for_invoice(credit_note, self.po)
        self.assertIn(credit_item.pk, suggestions)
        self.assertEqual(suggestions[credit_item.pk]["target_id"], self.po_item.pk)
        # Only 2 invoiced, so at most 2 creditable despite 5 on the credit line.
        self.assertEqual(suggestions[credit_item.pk]["quantity"], Decimal("2"))

    def test_po_items_with_pending_for_credit_returns_invoiced_pool(self):
        self._allocate(self.invoice_item, "6")
        rows = POInvoiceMatchService.po_items_with_pending(self.po, for_credit=True)
        self.assertEqual(rows[0][1], Decimal("6"))
        rows = POInvoiceMatchService.po_items_with_pending(self.po, for_credit=False)
        self.assertEqual(rows[0][1], Decimal("4"))


class CreditNotePaymentStatusTest(CreditNoteTestBase):
    def _apply_payment(self, invoice, amount):
        from payments.models import Payment, PaymentAllocation

        payment = Payment.objects.create(
            company=self.company,
            amount=Money(abs(amount), "EUR"),
            payment_date=date(2026, 6, 25),
        )
        PaymentAllocation.objects.create(
            payment=payment,
            content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk,
            amount_applied=Decimal(str(amount)),
        )

    def test_negative_total_invoice_unpaid_then_paid(self):
        credit_note = self._make_credit_note("A-001")
        Invoice.objects.filter(pk=credit_note.pk).update(total_amount=Decimal("-50.00"))
        credit_note.refresh_from_db()

        credit_note.recalculate_payment_status()
        self.assertEqual(credit_note.payment_status, InvoicePaymentStatus.UNPAID)

        self._apply_payment(credit_note, -20)
        credit_note.recalculate_payment_status()
        self.assertEqual(credit_note.payment_status, InvoicePaymentStatus.PARTIAL)

        self._apply_payment(credit_note, -30)
        credit_note.recalculate_payment_status()
        self.assertEqual(credit_note.payment_status, InvoicePaymentStatus.PAID)
