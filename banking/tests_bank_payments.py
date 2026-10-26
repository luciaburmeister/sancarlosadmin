"""
Paying purchase invoices from the bank through Arcopay PSD2 (BankPaymentService).

Only from a connected account with a valid consent, to the provider's
default IBAN, with the invoice's payment reference leading the concept. When
Arcopay reports the order done, each invoice gets a confirmed payment
expecting its reference, and the synced bank movement links to it.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from djmoney.money import Money

from banking.models import (
    BankPaymentOrder,
    BankPaymentOrderLine,
    BankPaymentOrderStatus,
    BankTransaction,
    ReconciliationStatus,
)
from banking.services.arcopay_import import ArcopayAPIError, ArcopayService
from banking.services.bank_payments import BankPaymentService
from banking.tasks import run_matcher_for_transaction
from company.models import ConsentStatus
from invoices.models import InvoiceDocumentType, PaymentMethod as InvoicePaymentMethod
from invoices.tests.factories import make_bank_account, make_company, make_provider, make_purchase_invoice
from materials.models import ProviderBankAccount
from payments.models import Payment, PaymentDirection

SOURCE_IBAN = "ES0000000000000000001111"
PROVIDER_IBAN = "ES9121000418450200051332"


def _return_url(order):
    return f"https://erp.test/return/{order.token}/"


class BankPaymentTestMixin:
    def setUp(self):
        super().setUp()
        self.company = make_company(name="Aluminios", legal_name="Aluminios SL")
        self.account = make_bank_account(
            company=self.company, iban=SOURCE_IBAN, holder_name="ALUMINIOS SL",
            nuek_token="tok", nuek_service="Santander", consent_status=ConsentStatus.VALID,
        )
        self.provider = make_provider(company=self.company, name="Perfiles SA")
        ProviderBankAccount.objects.create(
            company=self.company, provider=self.provider, bank_name="BBVA", iban=PROVIDER_IBAN,
            holder_name="Perfiles, S.A.", is_default=True,
        )
        self.invoice = make_purchase_invoice(company=self.company, provider=self.provider, total="1210.00",
                                             number="F/2026-077")
        self.user = User.objects.create_superuser("payer", "payer@example.com", "pw")

    def initiate(self, amounts=None, consent=ConsentStatus.VALID, **arcopay):
        amounts = amounts or {self.invoice: Decimal("1210.00")}
        result = {"follow": "https://bank.test/sign", "paymentId": "pay-1"}
        with patch.object(ArcopayService, "check_consent_status", return_value=consent), \
                patch.object(ArcopayService, "initiate_direct_payment", return_value=result, **arcopay) as direct, \
                patch.object(ArcopayService, "initiate_bulk_payment", return_value=result, **arcopay) as bulk:
            self.direct, self.bulk = direct, bulk
            return BankPaymentService.initiate(
                user=self.user, bank_account=self.account, amounts=amounts, return_url_for=_return_url,
            )

    def settle(self, order, status="OK"):
        with self.captureOnCommitCallbacks(execute=True):
            return BankPaymentService.apply_bank_status(order, status)


class ConceptAndEligibilityTests(BankPaymentTestMixin, TestCase):
    def test_concept_leads_with_the_payment_reference(self):
        concept = BankPaymentService.concept(self.invoice)

        self.assertTrue(concept.startswith(self.invoice.payment_reference))
        self.assertIn("F/2026-077", concept)

    def test_concept_is_sepa_safe_and_at_most_140(self):
        self.invoice.invoice_number = "Nº_" + "9" * 200
        concept = BankPaymentService.concept(self.invoice)

        self.assertLessEqual(len(concept), 140)
        self.assertNotIn("º", concept)
        self.assertNotIn("_", concept)
        self.assertTrue(concept.startswith(self.invoice.payment_reference))

    def test_payable_invoice(self):
        self.assertIsNone(BankPaymentService.ineligibility_reason(self.invoice))

    def test_credit_note_is_refused(self):
        self.invoice.document_type = InvoiceDocumentType.CREDIT_NOTE
        self.assertIsNotNone(BankPaymentService.ineligibility_reason(self.invoice))

    def test_direct_debit_invoice_is_refused(self):
        # The provider collects it: a transfer would pay it twice
        self.invoice.payment_method = InvoicePaymentMethod.SEPA_DIRECT_DEBIT
        self.assertIsNotNone(BankPaymentService.ineligibility_reason(self.invoice))

    def test_provider_without_a_clear_iban_is_refused(self):
        self.provider.bank_accounts.update(is_default=False)
        ProviderBankAccount.objects.create(
            company=self.company, provider=self.provider, bank_name="Otro", iban="ES7620770024003102575766",
        )
        self.assertIsNotNone(BankPaymentService.ineligibility_reason(self.invoice))

    def test_only_accounts_with_a_valid_consent_are_sources(self):
        make_bank_account(company=self.company, iban="ES0000000000000000003333", nuek_token="t2",
                          consent_status=ConsentStatus.INVALID)
        make_bank_account(company=self.company, iban="ES0000000000000000004444")

        self.assertEqual(list(BankPaymentService.source_accounts(self.company)), [self.account])

    def test_no_bank_payments_without_a_consented_account(self):
        self.account.consent_status = ConsentStatus.INVALID
        self.account.save()

        self.assertFalse(BankPaymentService.can_pay_from_bank(self.user, self.company))

    def test_no_bank_payments_without_the_permission(self):
        clerk = User.objects.create_user("clerk", "clerk@example.com", "pw")

        self.assertFalse(BankPaymentService.can_pay_from_bank(clerk, self.company))


class InitiateTests(BankPaymentTestMixin, TestCase):
    def test_single_invoice_is_a_direct_payment(self):
        order = self.initiate()

        self.direct.assert_called_once()
        self.bulk.assert_not_called()
        sent = self.direct.call_args.kwargs
        self.assertEqual(sent["bank_service"], "Santander")
        self.assertEqual(sent["source_iban"], SOURCE_IBAN)
        self.assertEqual(sent["destination_iban"], PROVIDER_IBAN)
        self.assertEqual(sent["amount"], Decimal("1210.00"))
        self.assertTrue(sent["description"].startswith(self.invoice.payment_reference))
        self.assertIn(str(order.token), sent["callback_url"])
        self.assertEqual(sent["redirect_url"], _return_url(order))
        self.assertEqual(order.status, BankPaymentOrderStatus.PENDING)
        self.assertEqual((order.arcopay_payment_id, order.follow_url), ("pay-1", "https://bank.test/sign"))

    def test_several_invoices_are_one_bulk_payment(self):
        other = make_purchase_invoice(company=self.company, provider=self.provider, total="100.00", number="F-2")

        order = self.initiate({self.invoice: Decimal("1210.00"), other: Decimal("40.00")})

        self.bulk.assert_called_once()
        items = self.bulk.call_args.kwargs["payments"]
        self.assertEqual(
            sorted((item["amount"], item["paymentDescription"][:12]) for item in items),
            sorted([(Decimal("1210.00"), self.invoice.payment_reference), (Decimal("40.00"), other.payment_reference)]),
        )
        self.assertEqual(order.amount, Money("1250.00", "EUR"))
        self.assertEqual(order.lines.count(), 2)

    def test_partial_amount(self):
        order = self.initiate({self.invoice: Decimal("500.00")})

        self.assertEqual(order.lines.get().amount, Money("500.00", "EUR"))

    def test_more_than_outstanding_is_refused(self):
        with self.assertRaises(ValidationError):
            self.initiate({self.invoice: Decimal("1210.01")})
        self.assertFalse(BankPaymentOrder.objects.exists())

    def test_consent_found_dead_at_the_bank_stops_it(self):
        with self.assertRaises(ValidationError):
            self.initiate(consent=ConsentStatus.INVALID)
        self.direct.assert_not_called()
        self.assertFalse(BankPaymentOrder.objects.exists())

    def test_invoice_of_another_company_is_refused(self):
        other_company = make_company(name="Otra")
        provider = make_provider(company=other_company)
        foreign = make_purchase_invoice(company=other_company, provider=provider, total="10.00")

        with self.assertRaises(ValidationError):
            self.initiate({foreign: Decimal("10.00")})

    def test_invoice_awaiting_signature_cannot_be_sent_again(self):
        self.initiate()

        with self.assertRaises(ValidationError):
            self.initiate()
        self.assertEqual(BankPaymentOrder.objects.count(), 1)

    def test_arcopay_refusal_is_kept_as_an_error(self):
        with self.assertRaises(ValidationError):
            self.initiate(side_effect=ArcopayAPIError("Formato de destinationIBAN incorrecto", "P002"))

        order = BankPaymentOrder.objects.get()
        self.assertEqual((order.status, order.error_code), (BankPaymentOrderStatus.ERROR, "P002"))
        # Not pending: the invoice can be sent again once fixed
        self.assertIsNone(BankPaymentService.ineligibility_reason(self.invoice, BankPaymentService.invoices_in_pending_orders([self.invoice.pk])))

    def test_unknown_service_is_looked_up_by_iban(self):
        self.account.nuek_service = ""
        self.account.save()
        with patch.object(ArcopayService, "iban_to_service", return_value=["Bankinter"]):
            self.initiate()

        self.account.refresh_from_db()
        self.assertEqual(self.account.nuek_service, "Bankinter")
        self.assertEqual(self.direct.call_args.kwargs["bank_service"], "Bankinter")

    def test_ambiguous_service_asks_to_reconnect(self):
        self.account.nuek_service = ""
        self.account.save()
        with patch.object(ArcopayService, "iban_to_service", return_value=["Santander", "SantanderEmpresas"]):
            with self.assertRaises(ValidationError):
                self.initiate()


class SettlementTests(BankPaymentTestMixin, TestCase):
    def test_ok_registers_a_payment_expecting_the_reference(self):
        order = self.initiate()

        self.settle(order)

        order.refresh_from_db()
        self.assertEqual(order.status, BankPaymentOrderStatus.COMPLETED)
        payment = order.lines.get().payment
        self.assertEqual(payment.direction, PaymentDirection.OUTBOUND)
        self.assertEqual(payment.bank_account, self.account)
        self.assertEqual(payment.expected_bank_reference, self.invoice.payment_reference)
        self.assertEqual(payment.amount, Money("1210.00", "EUR"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.payment_status, "paid")

    def test_synced_movement_links_to_that_payment(self):
        order = self.initiate()
        self.settle(order)
        line = order.lines.get()
        payments = Payment.objects.count()
        txn = BankTransaction.objects.create(
            company=self.company, bank_account=self.account, external_id="t1",
            transaction_date=timezone.localdate(), amount=Money("-1210.00", "EUR"), currency="EUR",
            description=f"TRANSFERENCIA A PERFILES SA {line.concept}", counterparty_name="PERFILES SA",
            counterparty_iban=PROVIDER_IBAN, raw_data={},
            reconciliation_status=ReconciliationStatus.UNRECONCILED,
        )

        result = run_matcher_for_transaction(txn.pk)

        self.assertTrue(result["auto_confirmed"])
        txn.refresh_from_db()
        self.assertEqual(txn.reconciliation.payment, line.payment)
        self.assertEqual(Payment.objects.count(), payments)

    def test_rejected_registers_nothing_and_frees_the_invoice(self):
        order = self.initiate()

        self.settle(order, "RJCT")

        order.refresh_from_db()
        self.assertEqual(order.status, BankPaymentOrderStatus.REJECTED)
        self.assertFalse(Payment.objects.exists())
        self.assertIsNone(BankPaymentService.ineligibility_reason(
            self.invoice, BankPaymentService.invoices_in_pending_orders([self.invoice.pk]),
        ))

    def test_unknown_status_keeps_it_pending(self):
        order = self.initiate()

        self.settle(order, "PDNG")

        order.refresh_from_db()
        self.assertEqual((order.status, order.bank_status), (BankPaymentOrderStatus.PENDING, "PDNG"))

    def test_abandoned_order_expires(self):
        order = self.initiate()
        BankPaymentOrder.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(days=8))

        self.settle(order, "PDNG")

        order.refresh_from_db()
        self.assertEqual(order.status, BankPaymentOrderStatus.EXPIRED)

    def test_settling_twice_registers_one_payment(self):
        order = self.initiate()

        self.settle(order)
        self.settle(order)

        self.assertEqual(Payment.objects.count(), 1)

    def test_what_was_paid_meanwhile_stays_as_credit(self):
        order = self.initiate()
        # Someone registered 1,000 by hand while the order waited
        self.settle_by_hand("1000.00")

        self.settle(order)

        payment = order.lines.get().payment
        self.assertEqual(payment.allocations.get().amount_applied, Money("210.00", "EUR"))
        self.assertEqual(payment.amount_pending_allocation, Money("1000.00", "EUR"))

    def settle_by_hand(self, amount):
        from django.contrib.contenttypes.models import ContentType

        from payments.models import PaymentAllocation, PaymentMethod, PaymentStatus

        with self.captureOnCommitCallbacks(execute=True):
            payment = Payment.objects.create(
                company=self.company, payment_date=timezone.localdate(), amount=Money(amount, "EUR"),
                direction=PaymentDirection.OUTBOUND, method=PaymentMethod.TRANSFER, status=PaymentStatus.CONFIRMED,
                bank_account=self.account,
            )
            PaymentAllocation.objects.create(
                payment=payment, content_type=ContentType.objects.get_for_model(self.invoice),
                object_id=self.invoice.pk, amount_applied=Money(amount, "EUR"),
            )


class CallbackTests(BankPaymentTestMixin, TestCase):
    def test_callback_asks_arcopay_and_settles(self):
        order = self.initiate()

        with patch.object(ArcopayService, "get_payment_status", return_value={"status": "OK"}) as status, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/banking/arcopay/payment-callback/?order={order.token}",
                {"token": "x", "paymentId": "pay-1"},
            )

        self.assertEqual(response.json(), {"result": "OK"})
        status.assert_called_once_with("pay-1")
        order.refresh_from_db()
        self.assertEqual(order.status, BankPaymentOrderStatus.COMPLETED)

    def test_callback_body_alone_settles_nothing(self):
        order = self.initiate()

        with patch.object(ArcopayService, "get_payment_status", return_value={"status": "PDNG"}):
            self.client.post(
                f"/api/banking/arcopay/payment-callback/?order={order.token}", {"status": "OK", "paymentId": "pay-1"},
            )

        order.refresh_from_db()
        self.assertEqual(order.status, BankPaymentOrderStatus.PENDING)

    def test_callback_for_another_payment_is_refused(self):
        order = self.initiate()

        with patch.object(ArcopayService, "get_payment_status") as status:
            response = self.client.post(
                f"/api/banking/arcopay/payment-callback/?order={order.token}", {"paymentId": "someone-else"},
            )

        self.assertEqual(response.status_code, 400)
        status.assert_not_called()

    def test_unknown_order(self):
        response = self.client.post("/api/banking/arcopay/payment-callback/?order=not-a-uuid", {})

        self.assertEqual(response.status_code, 404)

    def test_error_callback_is_recorded(self):
        order = self.initiate()

        with patch.object(ArcopayService, "get_payment_status", return_value={"status": "KO"}):
            self.client.post(
                f"/api/banking/arcopay/payment-callback-error/?order={order.token}",
                {"code": "P004", "message": "Saldo insuficiente", "paymentId": "pay-1"},
            )

        order.refresh_from_db()
        self.assertEqual((order.status, order.error_code), (BankPaymentOrderStatus.REJECTED, "P004"))


class PayFromBankScreenTests(BankPaymentTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def url(self, *invoices):
        from django.urls import reverse

        return f"{reverse('admin:banking_bankpaymentorder_pay')}?ids={','.join(str(i.pk) for i in invoices)}"

    def test_lists_payable_and_refused_invoices(self):
        credit = make_purchase_invoice(company=self.company, provider=self.provider, total="10.00", number="AB-1",
                                       document_type=InvoiceDocumentType.CREDIT_NOTE)

        response = self.client.get(self.url(self.invoice, credit))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["invoice"] for row in response.context["rows"]], [self.invoice])
        self.assertEqual([invoice for invoice, _ in response.context["refused"]], [credit])

    def test_post_sends_the_user_to_sign_at_the_bank(self):
        result = {"follow": "https://bank.test/sign", "paymentId": "pay-1"}
        with patch.object(ArcopayService, "check_consent_status", return_value=ConsentStatus.VALID), \
                patch.object(ArcopayService, "initiate_direct_payment", return_value=result):
            response = self.client.post(
                self.url(self.invoice), {"bank_account": self.account.pk, f"amount_{self.invoice.pk}": "1210.00"},
            )

        self.assertRedirects(response, "https://bank.test/sign", fetch_redirect_response=False)
        self.assertTrue(BankPaymentOrderLine.objects.filter(invoice=self.invoice).exists())

    def test_without_a_consented_account_it_goes_back(self):
        self.account.consent_status = ConsentStatus.INVALID
        self.account.save()

        response = self.client.get(self.url(self.invoice))

        self.assertEqual(response.status_code, 302)

    def test_invoice_page_offers_it_only_with_a_consented_account(self):
        from django.urls import reverse

        change_url = reverse("admin:invoices_invoice_change", args=[self.invoice.pk])
        action_url = reverse("admin:invoices_invoice_pay_from_bank_detail_action", args=[self.invoice.pk])

        self.assertContains(self.client.get(change_url), action_url)
        self.account.consent_status = ConsentStatus.INVALID
        self.account.save()
        self.assertNotContains(self.client.get(change_url), action_url)

    def test_order_page_renders(self):
        from django.urls import reverse

        order = self.initiate()

        response = self.client.get(reverse("admin:banking_bankpaymentorder_change", args=[order.pk]))

        self.assertContains(response, reverse("admin:banking_bankpaymentorder_sign_order_action", args=[order.pk]))


class PermissionTests(BankPaymentTestMixin, TestCase):
    """Sending payments from the bank needs its own permission, on top of seeing the invoices."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import Permission

        invoice_perms = Permission.objects.filter(
            content_type__app_label="invoices", codename__in=["view_invoice", "change_invoice"],
        )
        self.clerk = User.objects.create_user("clerk", "clerk@example.com", "pw", is_staff=True)
        self.clerk.user_permissions.add(*invoice_perms)
        self.payer = User.objects.create_user("treasurer", "treasurer@example.com", "pw", is_staff=True)
        self.payer.user_permissions.add(
            *invoice_perms,
            Permission.objects.get(content_type__app_label="banking", codename="can_initiate_bank_payment"),
        )
        from company.models import CompanyUser

        for user in (self.clerk, self.payer):
            CompanyUser.objects.create(user=user, company=self.company)

    def urls(self):
        from django.urls import reverse

        return (
            reverse("admin:invoices_invoice_change", args=[self.invoice.pk]),
            reverse("admin:invoices_invoice_pay_from_bank_detail_action", args=[self.invoice.pk]),
            f"{reverse('admin:banking_bankpaymentorder_pay')}?ids={self.invoice.pk}",
        )

    def test_without_the_permission_there_is_no_button_and_no_screen(self):
        change_url, action_url, pay_url = self.urls()
        self.client.force_login(self.clerk)

        self.assertNotContains(self.client.get(change_url), action_url)
        self.assertEqual(self.client.get(pay_url).status_code, 403)
        self.assertEqual(
            self.client.post(pay_url, {"bank_account": self.account.pk, f"amount_{self.invoice.pk}": "1210.00"}).status_code,
            403,
        )
        self.assertFalse(BankPaymentOrder.objects.exists())

    def test_without_the_permission_the_service_refuses(self):
        from django.core.exceptions import PermissionDenied

        self.user = self.clerk
        with self.assertRaises(PermissionDenied):
            self.initiate()

    def test_with_the_permission_it_is_offered(self):
        change_url, action_url, pay_url = self.urls()
        self.client.force_login(self.payer)

        self.assertContains(self.client.get(change_url), action_url)
        self.assertEqual(self.client.get(pay_url).status_code, 200)
