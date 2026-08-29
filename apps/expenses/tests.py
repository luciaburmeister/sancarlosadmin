from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from djmoney.money import Money

from company.models import Company
from invoices.models import InvoicePaymentStatus
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod
from apps.expenses.models import ExpenseClaim, ExpenseClaimStatus, ExpenseTicket
from apps.expenses.services.expense_ticket_service import ExpenseTicketService
from apps.expenses.services.ocr_services.ocr_interface import TicketOcrInterface, TicketData

User = get_user_model()


class FakeOcrProcessor(TicketOcrInterface):
    def __init__(self, ticket_data: TicketData):
        self.ticket_data = ticket_data

    def extract_data(self, file_path: str) -> TicketData:
        return self.ticket_data


def make_ticket_data(**overrides) -> TicketData:
    defaults = dict(
        vendor_name="Autopista del Sol",
        vendor_tax_id="A81432353",
        vendor_address=None,
        vendor_city=None,
        vendor_country="ES",
        date_of_purchase="2026-07-03",
        amount=Decimal("9.25"),
        currency="EUR",
        ticket_number=None,
        raw_response={},
    )
    defaults.update(overrides)
    return TicketData(**defaults)


class ExpenseTicketConflictTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.user = User.objects.create_user(username="driver")

    def _make_ticket(self):
        # `photo` just needs a truthy name — process_existing_ticket() only checks
        # for its presence; the real file path is supplied explicitly below so
        # nothing ever needs to be read from disk.
        return ExpenseTicket.objects.create(company=self.company, submitted_by=self.user, photo="tickets/fake.jpg")

    def _process(self, ticket, ticket_data):
        service = ExpenseTicketService(FakeOcrProcessor(ticket_data))
        return service.process_existing_ticket(ticket, file_path="fake/path.jpg")

    def test_same_vendor_date_amount_but_different_ticket_number_is_not_a_conflict(self):
        existing = self._make_ticket()
        self._process(existing, make_ticket_data(ticket_number="1593295"))

        new_ticket = self._make_ticket()
        result = self._process(new_ticket, make_ticket_data(ticket_number="870207"))

        self.assertTrue(result.success)
        self.assertFalse(result.is_duplicate)

    def test_same_vendor_date_amount_and_ticket_number_is_a_conflict(self):
        existing = self._make_ticket()
        self._process(existing, make_ticket_data(ticket_number="1593295"))

        new_ticket = self._make_ticket()
        result = self._process(new_ticket, make_ticket_data(ticket_number="1593295"))

        self.assertFalse(result.success)
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.duplicate_of, existing)

    def test_same_vendor_date_amount_without_ticket_number_falls_back_to_old_behavior(self):
        existing = self._make_ticket()
        self._process(existing, make_ticket_data(ticket_number=None))

        new_ticket = self._make_ticket()
        result = self._process(new_ticket, make_ticket_data(ticket_number=None))

        self.assertFalse(result.success)
        self.assertTrue(result.is_duplicate)
        self.assertEqual(result.duplicate_of, existing)

    def test_different_amount_is_never_a_conflict_even_with_same_ticket_number(self):
        existing = self._make_ticket()
        self._process(existing, make_ticket_data(ticket_number="1593295"))

        new_ticket = self._make_ticket()
        result = self._process(
            new_ticket, make_ticket_data(ticket_number="1593295", amount=Decimal("15.00"))
        )

        self.assertTrue(result.success)
        self.assertFalse(result.is_duplicate)


class ExpenseClaimPaymentStatusTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.user = User.objects.create_user(username="traveller")
        self.claim = ExpenseClaim.objects.create(company=self.company, submitted_by=self.user)
        ExpenseTicket.objects.create(
            company=self.company,
            submitted_by=self.user,
            photo="tickets/fake.jpg",
            claim=self.claim,
            original_amount=Money(Decimal("100.00"), "EUR"),
        )
        self.claim.recalculate_total()

    def _allocate(self, amount):
        payment = Payment.objects.create(
            company=self.company,
            payment_date=date(2026, 7, 3),
            amount=Money(amount, "EUR"),
            direction=PaymentDirection.OUTBOUND,
            method=PaymentMethod.CASH,
        )
        PaymentAllocation.objects.create(
            payment=payment,
            amount_applied=Money(amount, "EUR"),
            content_type=ContentType.objects.get_for_model(ExpenseClaim),
            object_id=self.claim.pk,
        )
        self.claim.refresh_from_db()

    def test_claim_starts_unpaid(self):
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.UNPAID)

    def test_partial_allocation_marks_claim_partial(self):
        self._allocate(Decimal("40.00"))
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.PARTIAL)

    def test_full_allocation_marks_claim_paid(self):
        self._allocate(Decimal("100.00"))
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.PAID)

    def test_removing_the_allocation_returns_the_claim_to_unpaid(self):
        self._allocate(Decimal("100.00"))
        PaymentAllocation.objects.get(object_id=self.claim.pk).delete()
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.UNPAID)


class ExpenseClaimAdminPaymentDisplayTest(TestCase):
    """The claim change page has to show what was paid and what is still owed."""

    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.admin = User.objects.create_superuser(username="boss", password="pw", email="boss@example.com")
        self.client.force_login(self.admin)
        self.claim = ExpenseClaim.objects.create(company=self.company, submitted_by=self.admin)
        ExpenseTicket.objects.create(
            company=self.company,
            submitted_by=self.admin,
            photo="tickets/fake.jpg",
            claim=self.claim,
            original_amount=Money(Decimal("100.00"), "EUR"),
        )
        self.claim.recalculate_total()
        payment = Payment.objects.create(
            company=self.company,
            payment_date=date(2026, 7, 3),
            amount=Money(Decimal("40.00"), "EUR"),
            direction=PaymentDirection.OUTBOUND,
            method=PaymentMethod.CASH,
            reference="REF-1",
        )
        PaymentAllocation.objects.create(
            payment=payment,
            amount_applied=Money(Decimal("40.00"), "EUR"),
            content_type=ContentType.objects.get_for_model(ExpenseClaim),
            object_id=self.claim.pk,
        )
        self.claim.refresh_from_db()

    def _get(self, url_name, *args):
        # Admin URLs are language-prefixed by i18n_patterns, so the prefix -
        # not an Accept-Language header - decides the rendered language. Pin it
        # to English so the assertions can match untranslated labels. The
        # request has to run inside the override too, not just reverse():
        # LocaleMiddleware activates the URL's language and never restores it,
        # which would leave English active for every test that follows.
        with translation.override("en"):
            return self.client.get(reverse(url_name, args=args))

    def test_change_page_shows_totals_status_and_the_payments_behind_them(self):
        response = self._get("admin:expenses_expenseclaim_change", self.claim.pk)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Payment Tracking", body)
        self.assertIn("Partial", body)
        self.assertIn("100.00", body)   # claimed
        self.assertIn("40.00", body)    # reimbursed
        self.assertIn("60.00", body)    # outstanding
        self.assertIn("REF-1", body)    # the payment itself, via the payments tab

    def test_changelist_shows_the_outstanding_column(self):
        response = self._get("admin:expenses_expenseclaim_changelist")
        self.assertEqual(response.status_code, 200)
        self.assertIn("60.00", response.content.decode())


class ExpenseClaimRegisterPaymentTest(TestCase):
    """Reimbursing a claim without leaving its admin page."""

    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.finance = User.objects.create_superuser(username="finance", password="pw", email="fin@example.com")
        self.employee = User.objects.create_user(username="traveller")
        self.client.force_login(self.finance)
        self.claim = ExpenseClaim.objects.create(company=self.company, submitted_by=self.employee)
        ExpenseTicket.objects.create(
            company=self.company,
            submitted_by=self.employee,
            photo="tickets/fake.jpg",
            claim=self.claim,
            original_amount=Money(Decimal("100.00"), "EUR"),
        )
        self.claim.recalculate_total()
        self.claim.transition_to(ExpenseClaimStatus.SUBMITTED)
        self.claim.transition_to(ExpenseClaimStatus.APPROVED)

    def _get(self, url_name, *args):
        with translation.override("en"):
            return self.client.get(reverse(url_name, args=args))

    def _post(self, amount):
        with translation.override("en"):
            url = reverse("admin:expenses_expenseclaim_register_payment", args=[self.claim.pk])
            return self.client.post(url, {
                "amount": amount,
                "currency": "EUR",
                "payment_date": "2026-07-03",
                "method": PaymentMethod.TRANSFER,
                "reference": "REIMB-1",
            })

    def test_the_button_is_on_the_claim_page(self):
        response = self._get("admin:expenses_expenseclaim_change", self.claim.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/expenses/expenseclaim/{self.claim.pk}/register-payment/", response.content.decode())

    def test_the_button_is_hidden_once_there_is_nothing_left_to_pay(self):
        self._post("100.00")
        response = self._get("admin:expenses_expenseclaim_change", self.claim.pk)
        self.assertNotIn("register-payment", response.content.decode())

    def test_the_form_page_loads(self):
        response = self._get("admin:expenses_expenseclaim_register_payment", self.claim.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Register Payment", response.content.decode())

    def test_registering_a_partial_payment_creates_the_payment_and_its_allocation(self):
        response = self._post("40.00")
        self.assertEqual(response.status_code, 302)

        payment = Payment.objects.get()
        self.assertEqual(payment.amount, Money(Decimal("40.00"), "EUR"))
        self.assertEqual(payment.direction, PaymentDirection.OUTBOUND)
        self.assertEqual(payment.company, self.company)
        self.assertEqual(payment.reference, "REIMB-1")

        allocation = PaymentAllocation.objects.get()
        self.assertEqual(allocation.related_object, self.claim)

        self.claim.refresh_from_db()
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.PARTIAL)
        self.assertEqual(self.claim.total_reimbursed, Money(Decimal("40.00"), "EUR"))
        self.assertEqual(self.claim.amount_remaining, Money(Decimal("60.00"), "EUR"))

    def test_paying_the_full_amount_marks_the_claim_paid_and_reimbursed(self):
        self._post("100.00")
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(self.claim.status, ExpenseClaimStatus.REIMBURSED)

    def test_cannot_pay_more_than_is_outstanding(self):
        response = self._post("150.00")
        self.assertEqual(response.status_code, 200)  # redisplayed with the error
        self.assertFalse(Payment.objects.exists())
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.UNPAID)

    def test_the_claimant_cannot_reimburse_their_own_claim(self):
        self.employee.is_staff = True
        self.employee.save(update_fields=["is_staff"])
        self.client.force_login(self.employee)
        response = self._get("admin:expenses_expenseclaim_change", self.claim.pk)
        self.assertNotIn("register-payment", response.content.decode())

    def test_the_url_itself_is_closed_to_users_who_cannot_approve(self):
        # The action's permission check only hides the button; the view has to
        # refuse the request too.
        self.employee.is_staff = True
        self.employee.save(update_fields=["is_staff"])
        self.client.force_login(self.employee)
        response = self._get("admin:expenses_expenseclaim_register_payment", self.claim.pk)
        self.assertEqual(response.status_code, 403)

    def test_a_claim_that_is_not_approved_cannot_be_paid(self):
        draft = ExpenseClaim.objects.create(company=self.company, submitted_by=self.employee)
        response = self._get("admin:expenses_expenseclaim_register_payment", draft.pk)
        self.assertEqual(response.status_code, 403)
