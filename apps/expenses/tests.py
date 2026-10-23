from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation
from djmoney.money import Money

from invoices.models import InvoicePaymentStatus
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod
from payments.testing import CompanyAdminTestMixin, payment_form
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


class ExpenseTicketConflictTest(CompanyAdminTestMixin, TestCase):
    def setUp(self):
        super().setUp()
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


class ExpenseClaimPaymentStatusTest(CompanyAdminTestMixin, TestCase):
    def setUp(self):
        super().setUp()
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

    def test_removing_the_allocation_returns_the_claim_to_unpaid(self):
        self._allocate(Decimal("100.00"))
        PaymentAllocation.objects.get(object_id=self.claim.pk).delete()
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.UNPAID)


class ExpenseClaimAdminPaymentDisplayTest(CompanyAdminTestMixin, TestCase):
    """The claim change page has to show what was paid and what is still owed."""

    def setUp(self):
        super().setUp()
        self.admin = self.login_superuser(username="boss", email="boss@example.com")
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


class ExpenseClaimRegisterPaymentTest(CompanyAdminTestMixin, TestCase):
    """Reimbursing a claim without leaving its admin page."""

    def setUp(self):
        super().setUp()
        self.finance = self.login_superuser()
        self.employee = User.objects.create_user(username="traveller")
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

    def _pay(self, amount):
        return self._post(
            "admin:expenses_expenseclaim_register_payment", self.claim.pk,
            data=payment_form(amount=amount, reference="REIMB-1"),
        )

    def test_the_button_is_on_the_claim_page(self):
        response = self._get("admin:expenses_expenseclaim_change", self.claim.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/expenses/expenseclaim/{self.claim.pk}/register-payment/", response.content.decode())

    def test_the_button_is_hidden_once_there_is_nothing_left_to_pay(self):
        self._pay("100.00")
        response = self._get("admin:expenses_expenseclaim_change", self.claim.pk)
        self.assertNotIn("register-payment", response.content.decode())

    def test_the_form_page_loads(self):
        response = self._get("admin:expenses_expenseclaim_register_payment", self.claim.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Register Payment", response.content.decode())

    def test_registering_a_partial_payment_creates_the_payment_and_its_allocation(self):
        response = self._pay("40.00")
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
        self._pay("100.00")
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(self.claim.status, ExpenseClaimStatus.REIMBURSED)

    def test_cannot_pay_more_than_is_outstanding(self):
        response = self._pay("150.00")
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


class ExpenseClaimTicketsSectionTest(CompanyAdminTestMixin, TestCase):
    """The changelist dropdown showing which tickets make up each claim."""

    def setUp(self):
        super().setUp()
        self.admin = self.login_superuser(username="boss", email="boss@example.com")
        self.claim = ExpenseClaim.objects.create(
            company=self.company,
            submitted_by=self.admin,
            notes="Trip to the Madrid fair",
        )
        ExpenseTicket.objects.create(
            company=self.company,
            submitted_by=self.admin,
            photo="tickets/fake.jpg",
            claim=self.claim,
            vendor_name="Autopista del Sol",
            ticket_number="T-4471",
            date_of_purchase=date(2026, 7, 3),
            original_amount=Money(Decimal("9.25"), "EUR"),
            notes="Toll on the way to the fair",
        )
        self.claim.recalculate_total()

    def _get_changelist(self):
        # See CompanyAdminTestMixin for why the language is pinned through the
        # URL prefix rather than a header.
        return self._get("admin:expenses_expenseclaim_changelist")

    def test_the_dropdown_lists_each_ticket_with_its_value_and_notes(self):
        response = self._get_changelist()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Autopista del Sol", body)
        self.assertIn("T-4471", body)
        self.assertIn("9.25", body)
        self.assertIn("Toll on the way to the fair", body)
        self.assertIn("Trip to the Madrid fair", body)   # the claim's own notes

    def test_a_ticket_awaiting_review_is_flagged_in_the_dropdown(self):
        response = self._get_changelist()
        self.assertIn("Needs review", response.content.decode())

    def test_a_claim_without_tickets_says_so(self):
        ExpenseTicket.objects.filter(claim=self.claim).delete()
        self.assertIn("No tickets attached to this claim.", self._get_changelist().content.decode())

    def test_the_dropdown_does_not_add_a_query_per_claim(self):
        with CaptureQueriesContext(connection) as one_claim:
            self._get_changelist()
        for _i in range(3):
            claim = ExpenseClaim.objects.create(company=self.company, submitted_by=self.admin)
            ExpenseTicket.objects.create(
                company=self.company,
                submitted_by=self.admin,
                photo="tickets/fake.jpg",
                claim=claim,
                original_amount=Money(Decimal("5.00"), "EUR"),
            )
        with CaptureQueriesContext(connection) as four_claims:
            self._get_changelist()
        self.assertEqual(len(four_claims), len(one_claim))


class ExpenseClaimChangelistFilterTest(CompanyAdminTestMixin, TestCase):
    """Who submitted it, when, what was bought, how much."""

    def setUp(self):
        super().setUp()
        from materials.models import Provider

        self.admin = self.login_superuser(username="boss", email="boss@example.com")
        self.ana = User.objects.create_user(username="ana", email="ana@example.com")
        self.luis = User.objects.create_user(username="luis", email="luis@example.com")
        self.repsol = Provider.objects.create(company=self.company, name="Repsol")
        self.ana_claim = self._claim(self.ana, created=datetime(2026, 9, 30, 18, 0), bought=date(2026, 9, 2),
                                     amount="40.00", provider=self.repsol, reviewed=False)
        self.luis_claim = self._claim(self.luis, created=datetime(2026, 10, 15, 9, 0), bought=date(2026, 10, 10),
                                      amount="300.00", reviewed=True)
        self.admin_claim = self._claim(self.admin, created=datetime(2026, 11, 2, 9, 0), bought=date(2026, 11, 1),
                                       amount="12.00", reviewed=True)

    def _claim(self, user, *, created, bought, amount, provider=None, reviewed):
        claim = ExpenseClaim.objects.create(company=self.company, submitted_by=user)
        ExpenseClaim.objects.filter(pk=claim.pk).update(created_at=timezone.make_aware(created))
        ExpenseTicket.objects.create(
            company=self.company, submitted_by=user, photo="tickets/fake.jpg", claim=claim,
            date_of_purchase=bought, original_amount=Money(Decimal(amount), "EUR"), provider=provider,
            needs_manual_review=not reviewed,
        )
        claim.recalculate_total()
        return claim

    def _filtered(self, query):
        with translation.override("en"):
            response = self.client.get(reverse("admin:expenses_expenseclaim_changelist") + "?" + query)
        self.assertEqual(response.status_code, 200)
        return set(response.context["cl"].result_list)

    def test_by_one_of_several_submitters(self):
        self.assertEqual(
            self._filtered(f"submitted_by__id__in={self.ana.pk},{self.luis.pk}"), {self.ana_claim, self.luis_claim},
        )

    def test_by_several_statuses(self):
        ExpenseClaim.objects.filter(pk=self.luis_claim.pk).update(status=ExpenseClaimStatus.SUBMITTED)

        self.assertEqual(self._filtered("status=submitted&status=draft"),
                         {self.ana_claim, self.luis_claim, self.admin_claim})
        self.assertEqual(self._filtered("status=submitted"), {self.luis_claim})

    def test_creation_range_includes_the_whole_last_day(self):
        self.assertEqual(
            self._filtered("created_at_from=01/09/2026&created_at_to=30/09/2026"), {self.ana_claim},
        )

    def test_by_purchase_date_of_its_tickets(self):
        self.assertEqual(
            self._filtered("tickets__date_of_purchase_from=01/10/2026&tickets__date_of_purchase_to=31/10/2026"),
            {self.luis_claim},
        )

    def test_by_ticket_provider(self):
        self.assertEqual(self._filtered(f"tickets__provider__id__in={self.repsol.pk}"), {self.ana_claim})

    def test_by_amount_claimed(self):
        self.assertEqual(self._filtered("total_claimed_from=20&total_claimed_to=100"), {self.ana_claim})

    def test_with_tickets_pending_review(self):
        self.assertEqual(self._filtered("tickets_pending_review=yes"), {self.ana_claim})
        self.assertEqual(self._filtered("tickets_pending_review=no"), {self.luis_claim, self.admin_claim})
