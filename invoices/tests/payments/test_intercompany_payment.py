"""Another group company pays one of our provider invoices, and charges us a debit note for it."""
import time
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djmoney.money import Money

from accounting.models import BusinessUnit, IncomeType
from apps.einvoicing.models import EInvoicingAccount
from apps.einvoicing.services import EInvoicingService
from banking.models import BankReconciliation, BankTransaction, ReconciliationStatus
from company.active_company import ActiveCompany
from company.models import CompanyUser
from invoices.models import (
    IntercompanyPaymentStatus, InvoiceDocumentType, InvoicePaymentStatus, SalesInvoiceDocumentType,
    SalesInvoiceStatus,
)
from invoices.services.intercompany_payment_service import IntercompanyPaymentService
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tasks import attach_intercompany_debit_note_pdf
from invoices.tests.factories import make_bank_account, make_company, make_provider
from invoices.tests.intercompany_fixtures import PAID_ON, IntercompanyMixin
from payments.models import PaymentDirection, PaymentMethod, PaymentStatus
from sales.models import Client


class IntercompanyPaymentTests(IntercompanyMixin, TestCase):
    def test_paying_from_another_company_links_both_sides(self):
        record = self.register()

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(record.status, IntercompanyPaymentStatus.ACTIVE)

        # The payer: its bank payment, booked against what we owe it
        payer_payment = record.payer_payment
        self.assertEqual(payer_payment.company, self.payer)
        self.assertEqual(payer_payment.direction, PaymentDirection.OUTBOUND)
        self.assertEqual(payer_payment.bank_account, self.payer_bank)
        self.assertEqual(payer_payment.amount_pending_allocation, Money("0.00", "EUR"))
        self.assertEqual(payer_payment.expected_bank_reference, self.invoice.payment_reference)
        self.assertEqual(payer_payment.reference, self.invoice.payment_reference)

        # ...and its debit note to us: final, no VAT, still owed
        note = record.debit_note
        self.assertEqual(note.company, self.payer)
        self.assertEqual(note.document_type, SalesInvoiceDocumentType.INTERCOMPANY)
        self.assertEqual(note.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(note.invoice_number, f"SUP-{timezone.now().year}-00001")
        self.assertIsNone(note.proforma_number)
        self.assertEqual(note.date_issued, PAID_ON)
        self.assertEqual((note.tax_amount.amount, note.total_amount.amount), (Decimal("0"), Decimal("1210.00")))
        self.assertEqual(note.payment_status, InvoicePaymentStatus.UNPAID)
        self.assertTrue(note.exclude_from_payment_reminders)
        self.assertEqual(note.client.tax_identifier, "B11111111")
        self.assertEqual(note.client.company, self.payer)

        # Ours: the invoice settled with no money, and the debt to the payer
        settlement = record.settlement_payment
        self.assertEqual((settlement.company, settlement.method), (self.debtor, PaymentMethod.INTERCOMPANY))
        self.assertIsNone(settlement.bank_account)
        mirror = record.mirror_invoice
        self.assertEqual(mirror.company, self.debtor)
        self.assertEqual(mirror.document_type, InvoiceDocumentType.INTERCOMPANY)
        self.assertEqual(mirror.provider.tax_identifier, "B22222222")
        self.assertEqual(mirror.invoice_number, note.invoice_number)
        self.assertEqual(mirror.total_amount, Money("1210.00", "EUR"))
        self.assertEqual(mirror.payment_status, InvoicePaymentStatus.UNPAID)
        self.assertEqual(mirror.payment_reference, note.payment_reference)

    def test_the_debit_note_line_has_no_business_unit(self):
        BusinessUnit.objects.create(
            company=self.payer, code="CARP", name="Carpintería", income_type=IncomeType.GOODS, is_default=True,
        )

        record = self.register()

        line = record.debit_note.items.get()
        self.assertIsNone(line.business_unit)
        line.business_unit = BusinessUnit.objects.get(company=self.payer)
        line.save(update_fields=["business_unit"])
        line.refresh_from_db()
        self.assertIsNone(line.business_unit)

    def test_a_part_of_the_invoice_leaves_it_partially_paid(self):
        record = self.register("500.00")

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(record.mirror_invoice.total_amount, Money("500.00", "EUR"))

    def test_existing_client_and_provider_are_reused(self):
        client = Client.objects.create(company=self.payer, name="Aluminios (cliente)", tax_identifier="B11111111")
        provider = make_provider(company=self.debtor, name="Grupo B (prov.)", tax_identifier="B22222222")

        record = self.register()

        self.assertEqual(record.payer_client, client)
        self.assertEqual(record.creditor_provider, provider)

    def test_new_client_gets_no_payment_reminders(self):
        record = self.register()

        self.assertTrue(record.payer_client.exclude_from_payment_reminders)

    def test_debit_notes_have_their_own_series(self):
        self.register("100.00")
        second = self.register("100.00")

        year = timezone.now().year
        self.assertEqual(second.debit_note.invoice_number, f"SUP-{year}-00002")
        self.assertEqual(
            SalesInvoiceService._generate_next_invoice_data(self.payer)["number"], f"INV-{year}-00001",
        )

    def test_a_debit_note_cannot_be_dated_before_the_last_one(self):
        self.register("100.00")

        with self.assertRaises(ValidationError):
            self.register("100.00", payment_date=PAID_ON.replace(day=1))

    def test_refused(self):
        other_bank = make_bank_account(company=self.debtor, iban="ES0000000000000000003333")
        credit_note = type(self.invoice).objects.create(
            company=self.debtor, provider=self.provider, invoice_number="AB-1",
            document_type=InvoiceDocumentType.CREDIT_NOTE, total_amount=Money("-10.00", "EUR"),
        )
        no_nif = make_company(name="Sin NIF")
        cases = {
            "more than owed": {"amount": "1210.01"},
            "nothing": {"amount": "0"},
            "bank account of another company": {"bank_account": other_bank},
            "credit note": {"invoice": credit_note, "amount": "10.00"},
        }
        for name, fields in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                self.register(**fields)
        with self.assertRaises(PermissionDenied):
            self.register(payer_company=self.debtor)
        with self.assertRaises(PermissionDenied):
            self.register(payer_company=no_nif, bank_account=make_bank_account(company=no_nif, iban="ES01"))

    def test_only_from_a_company_the_user_can_see(self):
        user = User.objects.create_user("clerk", "c@example.com", "pw")
        user.user_permissions.add(Permission.objects.get(codename="can_register_intercompany_payment"))
        CompanyUser.objects.create(user=user, company=self.debtor)
        self.user = User.objects.get(pk=user.pk)

        self.assertFalse(IntercompanyPaymentService.can_register(self.user, self.invoice))
        with self.assertRaises(PermissionDenied):
            self.register()

        CompanyUser.objects.create(user=user, company=self.payer)
        cache.clear()  # memberships are cached per user
        self.user = User.objects.get(pk=user.pk)
        self.assertTrue(IntercompanyPaymentService.can_register(self.user, self.invoice))
        self.register()

    def test_without_the_permission(self):
        self.user = User.objects.create_user("noperm", "n@example.com", "pw")

        with self.assertRaises(PermissionDenied):
            self.register()

    def test_cancel_undoes_everything(self):
        record = self.register()
        mirror_id = record.mirror_invoice_id

        with self.captureOnCommitCallbacks(execute=True):
            IntercompanyPaymentService.cancel(record, self.user)

        record.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual(record.status, IntercompanyPaymentStatus.CANCELLED)
        self.assertEqual(self.invoice.payment_status, InvoicePaymentStatus.UNPAID)
        self.assertEqual(record.payer_payment.status, PaymentStatus.CANCELLED)
        self.assertEqual(record.settlement_payment.status, PaymentStatus.CANCELLED)
        self.assertEqual(record.debit_note.status, SalesInvoiceStatus.CANCELLED)
        self.assertFalse(type(self.invoice).objects.filter(pk=mirror_id).exists())
        # The invoice can be paid again
        self.register()

    def test_cancel_refused_once_the_bank_movement_is_reconciled(self):
        record = self.register()
        txn = BankTransaction.objects.create(
            company=self.payer, bank_account=self.payer_bank, external_id="t1", transaction_date=PAID_ON,
            amount=Money("-1210.00", "EUR"), currency="EUR", description="x", raw_data={},
            reconciliation_status=ReconciliationStatus.RECONCILED,
        )
        BankReconciliation.objects.create(bank_transaction=txn, payment=record.payer_payment, is_confirmed=True)

        with self.assertRaises(ValidationError):
            IntercompanyPaymentService.cancel(record, self.user)

    def test_cancel_refused_once_the_debt_is_being_repaid(self):
        record = self.register()
        self.reimburse(record, make_bank_account(company=self.debtor, iban="ES0000000000000000001111"))

        with self.assertRaises(ValidationError):
            IntercompanyPaymentService.cancel(record, self.user)

    def test_debit_note_never_goes_to_the_aeat(self):
        EInvoicingAccount.objects.create(company=self.payer, name="Verifacti")
        record = self.register()

        self.assertIsNone(EInvoicingService._resolve_account(record.debit_note))

    def test_debit_note_pdf_becomes_the_mirrors_file(self):
        record = self.register()

        with mock.patch(
            "invoices.services.invoice_pdf_download.InvoicePdfDownloadService.render_sales_invoice_pdf",
            return_value=b"%PDF-1.4 debit note",
        ):
            attach_intercompany_debit_note_pdf(record.pk)

        record.mirror_invoice.refresh_from_db()
        with record.mirror_invoice.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 debit note")


class IntercompanyPaymentAdminTests(IntercompanyMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        cache.clear()  # switchable companies are cached, from before these existed
        self.activate(self.debtor, self.payer)

    def activate(self, *companies):
        """Turn ``companies`` on in the switcher."""
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [company.pk for company in companies]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

    def test_not_from_a_company_switched_off(self):
        self.activate(self.debtor)

        url = reverse("admin:invoices_invoice_intercompany_payment", args=[self.invoice.pk])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_registering_from_the_invoice(self):
        url = reverse("admin:invoices_invoice_intercompany_payment", args=[self.invoice.pk])

        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {
            "payer_company": self.payer.pk, "bank_account": self.payer_bank.pk,
            "payment_date": PAID_ON.isoformat(), "amount": "1210.00", "reference": "",
        })

        self.assertRedirects(response, reverse("admin:invoices_invoice_change", args=[self.invoice.pk]),
                             fetch_redirect_response=False)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.payment_status, InvoicePaymentStatus.PAID)

    def test_a_paid_invoice_offers_no_intercompany_payment(self):
        self.register()

        url = reverse("admin:invoices_invoice_intercompany_payment", args=[self.invoice.pk])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_mirror_is_read_only_and_the_links_show(self):
        record = self.register()

        response = self.client.get(reverse("admin:invoices_invoice_change", args=[record.mirror_invoice_id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, record.debit_note.invoice_number)
        self.assertFalse(response.context["has_change_permission"])

    def test_register_payment_offers_no_intercompany_method(self):
        response = self.client.get(reverse("admin:invoices_invoice_register_payment", args=[self.invoice.pk]))

        methods = [value for value, _label in response.context["form"].fields["method"].choices]
        self.assertNotIn(PaymentMethod.INTERCOMPANY, methods)

    def test_journal_entry_of_the_debit_note_and_the_mirror_is_their_payments(self):
        record = self.register()
        entries = reverse("admin:accounting_journalentry_changelist")
        cases = {
            "salesinvoice": (record.debit_note_id, record.payer_payment_id),
            "invoice": (record.mirror_invoice_id, record.settlement_payment_id),
        }
        for model, (document_id, payment_id) in cases.items():
            with self.subTest(model):
                response = self.client.get(
                    reverse(f"admin:invoices_{model}_view_journal_entry_detail_action", args=[document_id]),
                )
                self.assertRedirects(response, f"{entries}?payment__id={payment_id}", fetch_redirect_response=False)

        response = self.client.get(
            reverse("admin:invoices_invoice_view_journal_entry_detail_action", args=[self.invoice.pk]),
        )
        self.assertRedirects(response, f"{entries}?purchase_invoice__id={self.invoice.pk}",
                             fetch_redirect_response=False)

    def test_its_payments_can_still_be_journalized(self):
        record = self.register()

        response = self.client.get(
            reverse("admin:payments_payment_journalize_detail_action", args=[record.settlement_payment_id]),
        )

        self.assertRedirects(
            response, reverse("admin:payments_payment_change", args=[record.settlement_payment_id]),
            fetch_redirect_response=False,
        )

    def test_cancel_from_its_admin(self):
        record = self.register()

        response = self.client.post(
            reverse("admin:invoices_intercompanypayment_cancel_detail_action", args=[record.pk]),
            {"_form_submitted": "True"},
        )

        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.status, IntercompanyPaymentStatus.CANCELLED)
