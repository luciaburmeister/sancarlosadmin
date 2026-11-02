"""
Credit notes (facturas rectificativas) reversing a whole issued invoice
(CreditNoteService):

- the credit note is the exact negative mirror of the invoice: every line
  (same source, expediente, business unit), the header discount and the
  taxes; it is issued at once in its own series and says why in its notes;
- what the invoice still had to collect is offset against it (a zero
  compensation payment), what the client already paid stays owed to them
  on the credit note, to offset against another invoice;
- the reversed items can be billed again: the expediente's summary and
  totals, a delivery order, the advances it deducted, its work entries;
- what cannot be reversed or cancelled once reversed;
- the Verifacti payload of a credit note (R-type, por diferencias).
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from djmoney.money import Money

from apps.einvoicing.models import EInvoicingAccount
from apps.einvoicing.providers.verifacti import VerifactiProvider
from invoices.models import (
    InvoicePaymentStatus,
    RectificationCode,
    RectificationReason,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
)
from invoices.services.credit_notes import CreditNoteService
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin, invoice_taxes, totals
from management.models import WorkEntry, WorkEntryType
from payments.models import (
    Payment,
    PaymentAllocation,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)


class CreditNoteMixin(AdvanceExpedienteMixin):
    def issue(self, invoice):
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        return invoice

    def issued_goods(self):
        """The expediente's 1,000.00 + IVA 21 % goods, issued."""
        return self.issue(self.invoice_goods())

    def reverse(self, invoice, reason=RectificationReason.PRODUCT_RETURN, detail=""):
        credit_note = CreditNoteService.reverse(invoice, reason=reason, detail=detail, send_email=False)
        credit_note.refresh_from_db()
        invoice.refresh_from_db()
        return credit_note

    def pay(self, invoice, amount):
        payment = Payment.objects.create(
            company=self.company, client=self.client_obj, payment_date=date(2026, 10, 1),
            amount=Money(amount, "EUR"), direction=PaymentDirection.INBOUND,
            method=PaymentMethod.TRANSFER, status=PaymentStatus.CONFIRMED,
        )
        PaymentAllocation.objects.create(
            payment=payment, content_type=ContentType.objects.get_for_model(SalesInvoice),
            object_id=invoice.pk, amount_applied=Money(amount, "EUR"),
        )
        invoice.recalculate_payment_status()
        return payment


class CreditNoteMirrorTests(CreditNoteMixin, TestCase):
    def test_mirrors_lines_taxes_and_totals(self):
        invoice = self.issued_goods()
        credit_note = self.reverse(invoice)

        self.assertEqual(credit_note.document_type, SalesInvoiceDocumentType.CREDIT_NOTE)
        self.assertEqual(credit_note.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(credit_note.rectifies, invoice)
        self.assertEqual(credit_note.rectification_code, RectificationCode.R1)
        self.assertEqual(credit_note.project_file, invoice.project_file)
        self.assertEqual(
            totals(credit_note), tuple(-amount for amount in totals(invoice)),
        )
        self.assertEqual(totals(invoice)[-1], Decimal("1210.00"))
        self.assertEqual(
            invoice_taxes(credit_note),
            {key: (-base, -amount) for key, (base, amount) in invoice_taxes(invoice).items()},
        )
        original_lines = list(invoice.items.order_by("pk"))
        mirrored = list(credit_note.items.order_by("pk"))
        self.assertEqual(
            [(line.source_id, line.project_file_id, line.business_unit_id, -line.line_total.amount)
             for line in original_lines],
            [(line.source_id, line.project_file_id, line.business_unit_id, line.line_total.amount)
             for line in mirrored],
        )
        self.assertEqual([-line.quantity for line in original_lines], [line.quantity for line in mirrored])
        self.assertTrue(invoice.is_reversed)

    def test_numbered_in_its_own_series(self):
        invoice = self.issued_goods()
        credit_note = self.reverse(invoice)
        self.assertTrue(credit_note.invoice_number.startswith("R-"))
        self.assertEqual(credit_note.incremental_id, 1)

        # The invoice series goes on from the invoice, as if no credit note was issued
        rebilled = self.issue(self.invoice_goods())
        self.assertEqual(rebilled.incremental_id, invoice.incremental_id + 1)
        self.assertTrue(rebilled.invoice_number.startswith("INV-"))

    def test_mirrors_the_header_discount(self):
        invoice = self.invoice_goods()
        invoice.discount_amount = Money("100.00", "EUR")
        invoice.discount_breakdown = [{"rule_name": "Descuento", "amount": "100.00", "base": "1000.00"}]
        invoice.save()
        invoice = self.issue(invoice)

        credit_note = self.reverse(invoice)
        self.assertEqual(totals(credit_note), tuple(-amount for amount in totals(invoice)))
        self.assertEqual(credit_note.discount_amount.amount, Decimal("-100.00"))
        self.assertEqual(credit_note.taxable_base.amount, Decimal("-900.00"))
        self.assertEqual(
            credit_note.discount_breakdown,
            [{"rule_name": "Descuento", "amount": "-100.00", "base": "-1000.00"}],
        )

    def test_notes_say_which_invoice_and_why(self):
        invoice = self.issued_goods()
        credit_note = self.reverse(
            invoice, reason=RectificationReason.OTHER, detail="El cliente devolvió la ventana.",
        )
        self.assertIn(invoice.invoice_number, credit_note.notes)
        self.assertIn(invoice.date_issued.strftime("%d/%m/%Y"), credit_note.notes)
        self.assertIn(str(RectificationReason.OTHER.label), credit_note.notes)
        self.assertIn("El cliente devolvió la ventana.", credit_note.notes)
        self.assertEqual(credit_note.rectification_detail, "El cliente devolvió la ventana.")


class CreditNoteSettlementTests(CreditNoteMixin, TestCase):
    def test_unpaid_invoice_is_offset_in_full(self):
        invoice = self.issued_goods()
        credit_note = self.reverse(invoice)

        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(credit_note.payment_status, InvoicePaymentStatus.PAID)
        payment = Payment.objects.get(method=PaymentMethod.COMPENSATION)
        self.assertEqual(payment.amount.amount, Decimal("0.00"))
        self.assertEqual(payment.amount_pending_allocation.amount, Decimal("0.00"))
        self.assertEqual(
            sorted(payment.allocations.values_list("amount_applied", flat=True)),
            [Decimal("-1210.00"), Decimal("1210.00")],
        )

    def test_paid_part_stays_owed_and_offsets_the_rebill(self):
        invoice = self.issued_goods()
        self.pay(invoice, Decimal("500.00"))
        credit_note = self.reverse(invoice)

        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(credit_note.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(credit_note.amount_remaining_decimal, Decimal("-500.00"))

        rebilled = self.issue(self.invoice_goods())
        CreditNoteService.offset(rebilled, credit_note, Decimal("500.00"))
        rebilled.refresh_from_db()
        credit_note.refresh_from_db()
        self.assertEqual(credit_note.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(rebilled.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(rebilled.amount_remaining_decimal, Decimal("710.00"))

    def test_offset_cannot_exceed_either_balance(self):
        invoice = self.issued_goods()
        self.pay(invoice, Decimal("500.00"))
        credit_note = self.reverse(invoice)
        rebilled = self.issue(self.invoice_goods())
        with self.assertRaises(ValidationError):
            CreditNoteService.offset(rebilled, credit_note, Decimal("500.01"))


class CreditNoteRebillingTests(CreditNoteMixin, TestCase):
    def test_goods_can_be_invoiced_again(self):
        invoice = self.issued_goods()
        self.assertFalse(self.summary()["can_invoice"])

        self.reverse(invoice)
        summary = self.summary()
        self.assertTrue(summary["can_invoice"])
        self.assertEqual(summary["goods_remaining_net"], Decimal("1000.00"))
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.total_invoiced.amount, Decimal("0.00"))

        rebilled = self.issue(self.invoice_goods())
        self.assertEqual(totals(rebilled)[-1], Decimal("1210.00"))
        self.assertFalse(self.summary()["can_invoice"])

    def test_mirrors_what_was_issued_even_when_lines_drifted(self):
        invoice = self.issued_goods()
        SalesInvoice.objects.filter(pk=invoice.pk).update(total_amount=Decimal("1209.99"))
        invoice.refresh_from_db()
        credit_note = self.reverse(invoice)
        self.assertEqual(credit_note.total_amount.amount, Decimal("-1209.99"))
        self.assertEqual(credit_note.payment_status, InvoicePaymentStatus.PAID)

    def test_only_live_invoices_bill_their_items(self):
        invoice = self.issued_goods()
        credit_note = self.reverse(invoice)
        live = set(SalesInvoice.objects.live().values_list("pk", flat=True))
        self.assertNotIn(invoice.pk, live)
        self.assertNotIn(credit_note.pk, live)

    def test_delivery_order_can_be_invoiced_again(self):
        order = self.make_delivery_order(config=self.config)
        invoice = self.issue(self.invoice_delivery_orders(order))
        order.refresh_from_db()
        self.assertTrue(DeliveryOrderService.has_active_invoice(order))

        self.reverse(invoice)
        order.refresh_from_db()
        self.assertFalse(DeliveryOrderService.has_active_invoice(order))
        self.assertIn(order, DeliveryOrderService.get_invoiceable_delivery_orders(self.project_file))

        rebilled = self.invoice_delivery_orders(order)
        order.refresh_from_db()
        self.assertEqual(order.sales_invoice, rebilled)

    def test_deducted_advance_is_given_back(self):
        advance = self.issue_advance()
        self.project_file.refresh_from_db()
        pending = self.project_file.advance_pending_amount.amount
        self.assertEqual(pending, Decimal("300.00"))

        rest = self.issue(self.rest_invoice())
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("0.00"))

        credit_note = self.reverse(rest)
        self.assertTrue(credit_note.items.filter(advance_line__invoice=advance, line_total__gt=0).exists())
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, pending)

        rebilled = self.issue(self.rest_invoice())
        self.assertEqual(totals(rebilled), totals(rest))

    def test_work_entries_unlock_unless_another_invoice_bills_them(self):
        entry_type = WorkEntryType.objects.create(company=self.company, name="Porte")
        entries = [
            WorkEntry.objects.create(
                company=self.company, project_file=self.project_file, entry_type=entry_type,
                description=f"Porte {n}", quantity=Decimal("1"), unit_price=Money("50.00", "EUR"),
                date=date(2026, 9, n),
            )
            for n in (1, 2)
        ]

        def invoice_of(*billed):
            invoice = SalesInvoice.objects.create(
                company=self.company, client=self.client_obj, project_file=self.project_file,
            )
            for entry in billed:
                SalesInvoiceItem.objects.create(
                    invoice=invoice, description=entry.description, quantity=Decimal("1"),
                    unit_price=Money("50.00", "EUR"), source_id=f"work_entry_{entry.pk}",
                )
            ProjectFileInvoiceService.lock_invoiced_work_entries(
                invoice.items.values_list("source_id", flat=True)
            )
            return invoice

        reversed_invoice = self.issue(invoice_of(*entries))
        invoice_of(entries[1])  # a pro forma still billing the second one
        self.reverse(reversed_invoice)

        for entry in entries:
            entry.refresh_from_db()
        self.assertEqual([entry.is_locked for entry in entries], [False, True])


class CreditNoteRefusalTests(CreditNoteMixin, TestCase):
    def test_cannot_reverse_twice(self):
        invoice = self.issued_goods()
        self.reverse(invoice)
        with self.assertRaises(ValidationError):
            self.reverse(invoice)

    def test_cannot_reverse_a_pro_forma(self):
        with self.assertRaises(ValidationError):
            self.reverse(self.invoice_goods())

    def test_cannot_reverse_an_advance_or_a_credit_note(self):
        credit_note = self.reverse(self.issued_goods())
        with self.assertRaises(ValidationError):
            self.reverse(credit_note)
        advance = self.issue_advance()
        with self.assertRaises(ValidationError):
            self.reverse(advance)

    def test_other_needs_a_detail(self):
        invoice = self.issued_goods()
        with self.assertRaises(ValidationError):
            self.reverse(invoice, reason=RectificationReason.OTHER)
        invoice.refresh_from_db()
        self.assertFalse(invoice.is_reversed)

    def test_neither_side_can_be_cancelled(self):
        invoice = self.issued_goods()
        credit_note = self.reverse(invoice)
        for document in (invoice, credit_note):
            with self.assertRaises(ValidationError):
                SalesInvoiceService.validate_for_cancel(document)

    def test_credit_note_cannot_be_duplicated(self):
        credit_note = self.reverse(self.issued_goods())
        with self.assertRaises(ValidationError):
            SalesInvoiceService.duplicate_invoice(credit_note)


class CreditNotePayloadTests(CreditNoteMixin, TestCase):
    def test_rectificativa_por_diferencias(self):
        invoice = self.issued_goods()
        credit_note = self.reverse(invoice)
        account = EInvoicingAccount(company=self.company, name="Verifacti", invoice_serie="INV")

        payload = VerifactiProvider()._build_create_payload(account, credit_note)

        self.assertEqual(payload["tipo_factura"], "R1")
        self.assertEqual(payload["tipo_rectificativa"], "I")
        fecha = invoice.date_issued.strftime("%d-%m-%Y")
        self.assertEqual(payload["fecha_operacion"], fecha)
        self.assertEqual(payload["facturas_rectificadas"], [
            {"serie": "INV", "numero": invoice.invoice_number, "fecha_expedicion": fecha},
        ])
        self.assertEqual(Decimal(payload["importe_total"]), Decimal("-1210.00"))
        self.assertEqual(
            [(Decimal(linea["base_imponible"]), Decimal(linea["cuota_repercutida"])) for linea in payload["lineas"]],
            [(Decimal("-1000.00"), Decimal("-210.00"))],
        )
        self.assertEqual(payload["descripcion"], credit_note.notes[:250])


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class CreditNoteAdminTests(CreditNoteMixin, TestCase):
    REVERSE = "admin:invoices_salesinvoice_reverse_invoice_detail_action"
    OFFSET = "admin:invoices_salesinvoice_offset_credit_note_detail_action"

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("rectifier", "r@x.com", "pw"))

    def test_reverse_dialog_issues_the_credit_note(self):
        invoice = self.issued_goods()
        response = self.client.post(reverse(self.REVERSE, args=[invoice.pk]), {
            "_form_submitted": "True",
            "reason": RectificationReason.INCORRECT_AMOUNT,
            "detail": "",
            "code": RectificationCode.R4,
        })
        credit_note = SalesInvoice.objects.get(rectifies=invoice)
        self.assertEqual(
            response["HX-Redirect"], reverse("admin:invoices_salesinvoice_change", args=[credit_note.pk]),
        )
        self.assertEqual(credit_note.rectification_code, RectificationCode.R4)

        for document in (invoice, credit_note):
            page = self.client.get(reverse("admin:invoices_salesinvoice_change", args=[document.pk]))
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, invoice.invoice_number)
            self.assertContains(page, credit_note.invoice_number)

    def test_reverse_dialog_needs_a_detail_for_other(self):
        invoice = self.issued_goods()
        self.client.post(reverse(self.REVERSE, args=[invoice.pk]), {
            "_form_submitted": "True", "reason": RectificationReason.OTHER, "detail": "", "code": "R1",
        })
        self.assertFalse(SalesInvoice.objects.filter(rectifies=invoice).exists())

    def test_offset_dialog_settles_the_rebill(self):
        invoice = self.issued_goods()
        self.pay(invoice, Decimal("500.00"))
        credit_note = self.reverse(invoice)
        rebilled = self.issue(self.invoice_goods())

        self.client.post(reverse(self.OFFSET, args=[credit_note.pk]), {
            "_form_submitted": "True", "invoice": rebilled.pk,
        })
        credit_note.refresh_from_db()
        self.assertEqual(credit_note.payment_status, InvoicePaymentStatus.PAID)
        rebilled.refresh_from_db()
        self.assertEqual(rebilled.amount_remaining_decimal, Decimal("710.00"))
