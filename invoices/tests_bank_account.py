from django.test import TestCase
from company.models import Company, BankAccount
from invoices.models import SalesInvoice
from sales.models import Client
from configurator.services.export.sales_invoice import SalesInvoiceExportStrategy

class BankAccountExportTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Global Corp")
        self.client = Client.objects.create(name="Test Client", company=self.company)
        self.invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client,
            subtotal_amount=100,
            total_amount=121
        )

    def test_default_bank_account_logic(self):
        # Create first bank account as default
        bank1 = BankAccount.objects.create(
            company=self.company,
            bank_name="Bank 1",
            account_number="ACC1",
            is_default=True
        )
        self.assertTrue(bank1.is_default)

        # Create second bank account as default
        bank2 = BankAccount.objects.create(
            company=self.company,
            bank_name="Bank 2",
            account_number="ACC2",
            is_default=True
        )
        
        # bank1 should no longer be default
        bank1.refresh_from_db()
        self.assertFalse(bank1.is_default)
        self.assertTrue(bank2.is_default)

    def test_export_context_includes_bank_account(self):
        # Create a default bank account
        BankAccount.objects.create(
            company=self.company,
            bank_name="International Bank",
            holder_name="Global Corp",
            account_number="12345678",
            routing_number="ROUT-99",
            country="US",
            is_default=True
        )

        strategy = SalesInvoiceExportStrategy(self.invoice)
        context = strategy.get_context()

        self.assertIn("bank_account", context)
        self.assertEqual(context["bank_account"]["bank_name"], "International Bank")
        self.assertEqual(context["bank_account"]["account_number"], "12345678")
        self.assertEqual(context["bank_account"]["routing_number"], "ROUT-99")
        self.assertEqual(context["bank_account"]["country"], "US")

    def test_export_context_without_bank_account(self):
        # No bank accounts created
        strategy = SalesInvoiceExportStrategy(self.invoice)
        context = strategy.get_context()

        self.assertNotIn("bank_account", context)
