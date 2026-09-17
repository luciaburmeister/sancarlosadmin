"""A client used by a company other than its own is either moved there, or
copied so both companies keep their own record and their own history.

    python manage.py test sales.tests.test_client_company_repair
"""
from datetime import date

from django.core.management import call_command
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from invoices.models import SalesInvoice, SalesInvoiceStatus
from sales.models import (
    Client,
    ClientAddress,
    ClientBankAccount,
    ClientContact,
    ContactRole,
    DeliveryOrder,
)
from sales.services.client_company_repair import (
    COPY,
    MERGE,
    MOVE,
    apply_plan,
    build_plans,
    find_mismatched_client_ids,
)


def make_invoice(company, client, number):
    return SalesInvoice.objects.create(
        company=company,
        client=client,
        invoice_number=number,
        total_amount=Money("100.00", "EUR"),
        date_issued=date(2026, 1, 1),
        due_date=date(2026, 2, 1),
        status=SalesInvoiceStatus.FINAL,
    )


class ClientCompanyRepairTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.owner = Company.objects.create(name="Aluares")
        cls.user = Company.objects.create(name="San Carlos")

    def _client(self, company, name="RECICAB 2005 S.L", tax_identifier="B29000001"):
        return Client.objects.create(
            company=company, name=name, tax_identifier=tax_identifier
        )

    def _repair(self, client_ids=None):
        plans = build_plans(client_ids or find_mismatched_client_ids())
        for plan in plans:
            apply_plan(plan)
        return plans

    def test_client_only_used_by_the_other_company_is_moved(self):
        client = self._client(self.owner)
        make_invoice(self.user, client, "2500584")

        plans = self._repair()

        self.assertEqual([a.kind for a in plans[0].actions], [MOVE])
        client.refresh_from_db()
        self.assertEqual(client.company, self.user)
        self.assertEqual(Client.objects.count(), 1)

    def test_a_moved_client_takes_its_subrecords_along(self):
        client = self._client(self.owner)
        address = ClientAddress.objects.create(
            client=client, company=self.owner, street="C/ Mayor 1",
            city="Málaga", postal_code="29001", province="Málaga",
        )
        account = ClientBankAccount.objects.create(
            client=client, company=self.owner, bank_name="BBVA", iban="ES7620770024003102575766"
        )
        make_invoice(self.user, client, "2500584")

        self._repair()

        address.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(address.company, self.user)
        self.assertEqual(account.company, self.user)

    def test_client_used_by_both_companies_is_copied(self):
        client = self._client(self.owner)
        ours = make_invoice(self.owner, client, "INV-2026-00119")
        theirs = make_invoice(self.user, client, "2500584")

        plans = self._repair()

        self.assertEqual([a.kind for a in plans[0].actions], [COPY])
        client.refresh_from_db()
        self.assertEqual(client.company, self.owner)

        copy = Client.objects.exclude(pk=client.pk).get()
        self.assertEqual(copy.company, self.user)
        self.assertEqual(copy.name, client.name)
        self.assertEqual(copy.tax_identifier, client.tax_identifier)

        ours.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(ours.client, client)
        self.assertEqual(theirs.client, copy)

    def test_the_copy_gets_its_own_subrecords(self):
        client = self._client(self.owner)
        address = ClientAddress.objects.create(
            client=client, company=self.owner, street="C/ Mayor 1",
            city="Málaga", postal_code="29001", province="Málaga",
        )
        account = ClientBankAccount.objects.create(
            client=client, company=self.owner, bank_name="BBVA", iban="ES7620770024003102575766"
        )
        client.default_client_bank_account = account
        client.save(update_fields=["default_client_bank_account"])
        ClientContact.objects.create(
            client=client, address=address, name="Ana", role=ContactRole.PURCHASING
        )
        make_invoice(self.owner, client, "INV-2026-00119")
        make_invoice(self.user, client, "2500584")

        self._repair()

        copy = Client.objects.exclude(pk=client.pk).get()
        copied_address = copy.addresses.get()
        copied_account = copy.client_bank_accounts.get()
        copied_contact = copy.contacts.get()
        self.assertEqual(copied_address.company, self.user)
        self.assertEqual(copied_address.street, address.street)
        self.assertEqual(copied_account.company, self.user)
        self.assertEqual(copied_account.iban, account.iban)
        self.assertEqual(copied_contact.address, copied_address)
        self.assertEqual(copy.default_client_bank_account, copied_account)
        # The original keeps exactly one of each.
        self.assertEqual(client.addresses.count(), 1)
        self.assertEqual(client.client_bank_accounts.count(), 1)

    def test_delivery_orders_follow_the_copied_address(self):
        client = self._client(self.owner)
        address = ClientAddress.objects.create(
            client=client, company=self.owner, street="C/ Mayor 1",
            city="Málaga", postal_code="29001", province="Málaga",
        )
        make_invoice(self.owner, client, "INV-2026-00119")
        delivery_order = DeliveryOrder.objects.create(
            company=self.user, client=client, delivery_address=address
        )

        self._repair()

        copy = Client.objects.exclude(pk=client.pk).get()
        delivery_order.refresh_from_db()
        self.assertEqual(delivery_order.client, copy)
        self.assertEqual(delivery_order.delivery_address.client, copy)
        self.assertEqual(delivery_order.delivery_address.street, address.street)
        self.assertNotEqual(delivery_order.delivery_address_id, address.pk)

    def test_documents_go_to_an_existing_client_with_the_same_tax_id(self):
        client = self._client(self.owner)
        twin = self._client(self.user, name="RECICAB 2005 SL")
        invoice = make_invoice(self.user, client, "2500584")

        plans = self._repair()

        self.assertEqual([a.kind for a in plans[0].actions], [MERGE])
        invoice.refresh_from_db()
        self.assertEqual(invoice.client, twin)
        self.assertEqual(Client.objects.count(), 2)
        client.refresh_from_db()
        self.assertEqual(client.company, self.owner)

    def test_merge_copies_only_the_address_a_delivery_order_needs(self):
        client = self._client(self.owner)
        twin = self._client(self.user, name="RECICAB 2005 SL")
        used = ClientAddress.objects.create(
            client=client, company=self.owner, street="C/ Mayor 1",
            city="Málaga", postal_code="29001", province="Málaga",
        )
        ClientAddress.objects.create(
            client=client, company=self.owner, street="C/ Larios 2",
            city="Málaga", postal_code="29005", province="Málaga",
        )
        delivery_order = DeliveryOrder.objects.create(
            company=self.user, client=client, delivery_address=used
        )

        self._repair()

        delivery_order.refresh_from_db()
        self.assertEqual(delivery_order.client, twin)
        self.assertEqual(
            list(twin.addresses.values_list("street", flat=True)), ["C/ Mayor 1"]
        )

    def test_a_matching_client_leaves_nothing_to_do(self):
        client = self._client(self.owner)
        make_invoice(self.owner, client, "INV-2026-00119")

        self.assertEqual(find_mismatched_client_ids(), [])
        self.assertEqual(build_plans(find_mismatched_client_ids()), [])

    def test_dry_run_changes_nothing(self):
        client = self._client(self.owner)
        make_invoice(self.user, client, "2500584")

        call_command("fix_cross_company_clients")

        client.refresh_from_db()
        self.assertEqual(client.company, self.owner)

        call_command("fix_cross_company_clients", "--apply")

        client.refresh_from_db()
        self.assertEqual(client.company, self.user)
