from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from apps.expenses.models import ExpenseTicket
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
