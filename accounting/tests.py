from django.test import TestCase
from accounting.models import Journal, JournalEntry, EntryStatus, CompanyAccount, JournalType, FiscalYear, CompanyChartOfAccounts, ChartOfAccountsTemplate, JournalEntryLine, AccountType, BalanceSide, TemplateAccount
from company.models import Company
from datetime import date
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _, activate


class JournalEntryNumberingTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Test Company', tax_identifier='B12345678', logo="logos/test.png")
        activate("es")
        self.chart_template = ChartOfAccountsTemplate.objects.create(name="PGCE 2008", country="ES")
        self.company_chart = CompanyChartOfAccounts.objects.create(company=self.company, template=self.chart_template)
        
        self.journal_gen = Journal.objects.create(company=self.company, code='GEN', name='General', type=JournalType.GENERAL)
        self.journal_bank = Journal.objects.create(company=self.company, code='BANK', name='Bank', type=JournalType.BANK)
        
        self.account = CompanyAccount.objects.create(company=self.company, code='572000', name='Bank', is_active=True)
        
        # Create an open Fiscal Year
        self.fy_2026 = FiscalYear.objects.create(
            company=self.company,
            name="FY 2026",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            is_closed=False
        )

    def create_balanced_entry(self, journal, date_val):
        entry = JournalEntry.objects.create(company=self.company, journal=journal, date=date_val, status=EntryStatus.DRAFT)
        JournalEntryLine.objects.create(company=self.company, entry=entry, account=self.account, debit=100, credit=0)
        JournalEntryLine.objects.create(company=self.company, entry=entry, account=self.account, debit=0, credit=100)
        return entry

    def test_numbering_sequence(self):
        # 1. First entry
        entry1 = self.create_balanced_entry(self.journal_gen, date(2026, 1, 1))
        entry1.post()
        self.assertEqual(entry1.number, "GEN/2026/00001")
        self.assertEqual(entry1.status, EntryStatus.POSTED)

        # 2. Second entry (same year)
        entry2 = self.create_balanced_entry(self.journal_gen, date(2026, 1, 5))
        entry2.post()
        self.assertEqual(entry2.number, "GEN/2026/00002")

        # 3. Third entry (different year) - requires new Fiscal Year?
        # Post checks for FiscalYear existence.
        FiscalYear.objects.create(
            company=self.company,
            name="FY 2027",
            start_date=date(2027, 1, 1),
            end_date=date(2027, 12, 31),
            is_closed=False
        )
        
        entry3 = self.create_balanced_entry(self.journal_gen, date(2027, 1, 1))
        entry3.post()
        self.assertEqual(entry3.number, "GEN/2027/00001")

    def test_journal_independence(self):
        entry1 = self.create_balanced_entry(self.journal_gen, date(2026, 1, 1))
        entry1.post()
        self.assertEqual(entry1.number, "GEN/2026/00001")

        entry2 = self.create_balanced_entry(self.journal_bank, date(2026, 1, 1))
        entry2.post()
        self.assertEqual(entry2.number, "BANK/2026/00001")

    def test_fiscal_year_closed(self):
        closed_fy = FiscalYear.objects.create(
            company=self.company,
            name="FY 2025",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            is_closed=True
        )
        entry = self.create_balanced_entry(self.journal_gen, date(2025, 6, 1))
        with self.assertRaisesMessage(ValidationError, str(_("Fiscal year FY 2025 is closed."))):
            entry.post()

    def test_lock_date(self):
        self.company_chart.lock_date = date(2026, 1, 31)
        self.company_chart.save()
        
        # Entry before lock date
        entry = self.create_balanced_entry(self.journal_gen, date(2026, 1, 15))
        with self.assertRaisesMessage(ValidationError, str(_("Cannot post entry before lock date"))):
            entry.post()
        
        # Entry after lock date should be fine
        entry2 = self.create_balanced_entry(self.journal_gen, date(2026, 2, 1))
        entry2.post()
        self.assertTrue(entry2.status == EntryStatus.POSTED)

    def test_modification_protection(self):
        entry = self.create_balanced_entry(self.journal_gen, date(2026, 1, 1))
        entry.post()
        
        # Try to modify date
        entry.date = date(2026, 1, 2)
        with self.assertRaisesMessage(ValidationError, str(_("Cannot modify a posted entry."))):
            entry.save() 
            
        # Try to modify status
        entry.refresh_from_db()
        entry.status = EntryStatus.DRAFT
        with self.assertRaisesMessage(ValidationError, str(_("Cannot modify a posted entry."))):
            entry.save() 
            
        # Try to modify journal
        entry.refresh_from_db()
        entry.journal = self.journal_bank
        with self.assertRaisesMessage(ValidationError, str(_("Cannot modify a posted entry."))):
            entry.save() 

        # Try to modify company
        entry.refresh_from_db()
        # We need another company to test this, sticking to Journal/Date/Status is enough coverage for now.
        
        # clean() alone, without save(), must refuse the change too
        entry.refresh_from_db()
        entry.date = date(2026, 1, 2)
        with self.assertRaisesMessage(ValidationError, str(_("Cannot modify a posted entry."))):
            entry.clean()

    def test_delete_protection(self):
        entry = self.create_balanced_entry(self.journal_gen, date(2026, 1, 1))
        entry.post()
        with self.assertRaisesMessage(ValidationError, str(_("Cannot delete a posted entry."))):
            entry.delete()

    def test_debit_xor_credit_constraint(self):
        entry = JournalEntry.objects.create(company=self.company, journal=self.journal_gen, date=date(2026, 1, 1), status=EntryStatus.DRAFT)
        
        # 1. Both > 0 (Already tested, fails integrity or clean)
        with self.assertRaises(ValidationError):
             line = JournalEntryLine(company=self.company, entry=entry, account=self.account, debit=100, credit=100)
             line.clean()

        # 2. Both 0 (Strict XOR should fail)
        with self.assertRaises(ValidationError):
             line = JournalEntryLine(company=self.company, entry=entry, account=self.account, debit=0, credit=0)
             line.clean()

        # 3. Negative values
        with self.assertRaises(ValidationError):
             line = JournalEntryLine(company=self.company, entry=entry, account=self.account, debit=-10, credit=0)
             line.clean()
        
        # 4. Valid line
        line = JournalEntryLine(company=self.company, entry=entry, account=self.account, debit=100, credit=0)
        line.clean()
        line.save()

    def test_fiscal_year_overlap(self):
        # fy_2026 exists: 2026-01-01 to 2026-12-31
        
        # Overlapping start
        fy_overlap = FiscalYear(
            company=self.company,
            name="Overlap Start",
            start_date=date(2025, 6, 1),
            end_date=date(2026, 6, 1)
        )
        with self.assertRaisesMessage(ValidationError, str(_("Fiscal year overlaps with an existing year."))):
            fy_overlap.clean()
            
        # Overlapping end
        fy_overlap_2 = FiscalYear(
            company=self.company,
            name="Overlap End",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 6, 1)
        )
        with self.assertRaisesMessage(ValidationError, str(_("Fiscal year overlaps with an existing year."))):
            fy_overlap_2.clean()
            
        # Contained
        fy_contained = FiscalYear(
            company=self.company,
            name="Contained",
            start_date=date(2026, 2, 1),
            end_date=date(2026, 3, 1)
        )
        with self.assertRaisesMessage(ValidationError, str(_("Fiscal year overlaps with an existing year."))):
            fy_contained.clean()

    def test_posted_entry_immutability(self):
        entry = self.create_balanced_entry(self.journal_gen, date(2026, 1, 1))
        entry.post()
        self.assertEqual(entry.status, EntryStatus.POSTED)
        self.assertIsNotNone(entry.posted_at)
        
        # Try to add a line
        line = JournalEntryLine(company=self.company, entry=entry, account=self.account, debit=100, credit=0)
        with self.assertRaisesMessage(ValidationError, str(_("Cannot add or modify lines of a posted entry."))):
            line.clean()
            
        # Try to modify existing line
        existing_line = entry.lines.first()
        existing_line.debit = 200
        with self.assertRaisesMessage(ValidationError, str(_("Cannot add or modify lines of a posted entry."))):
            existing_line.clean()
            
    def test_currency_consistency(self):
        # Account with currency constraint
        usd_account = CompanyAccount.objects.create(
            company=self.company, 
            code='572002', 
            name='Bank USD', 
            currency='USD',
            is_active=True
        )
        entry = JournalEntry.objects.create(company=self.company, journal=self.journal_gen, date=date(2026, 1, 1), status=EntryStatus.DRAFT)
        
        # Line with EUR (default) -> Fail
        line = JournalEntryLine(company=self.company, entry=entry, account=usd_account, debit=100, credit=0)
        # debit defaults to EUR. 
        with self.assertRaisesMessage(ValidationError, str(_("Debit currency (EUR) must match account currency (USD)."))):
            line.clean()
            
        # Line with USD -> Pass
        # We need to set currency on MoneyField. 
        # line.debit = Money(100, 'USD') --> requires importing Money or setting amount/currency fields if exposed?
        # Djmoney fields can be set with tuple or Money object.
        from djmoney.money import Money
        line.debit = Money(100, 'USD')
        line.credit = Money(0, 'USD') # Ensure credit matches too
        line.clean() # Should pass currency check


class ChartInstallerTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", tax_identifier="B12345678")
        self.template = ChartOfAccountsTemplate.objects.create(name="Test Template", country="ES")
        
    def test_infer_attributes(self):
        from accounting.services.chart_installer import PGCInferenceStrategy
        
        strategy = PGCInferenceStrategy()
        
        # Test Group 1
        ensure_type = lambda c, t, s: self.assertEqual(strategy.infer(c), (t, s))
        
        ensure_type("100", AccountType.EQUITY, BalanceSide.CREDIT)
        ensure_type("170", AccountType.LIABILITY, BalanceSide.CREDIT)
        
        # Test Group 2
        ensure_type("210", AccountType.ASSET, BalanceSide.DEBIT)
        
        # Test Group 3
        ensure_type("300", AccountType.ASSET, BalanceSide.DEBIT)
        
        # Test Group 4
        ensure_type("400", AccountType.LIABILITY, BalanceSide.CREDIT)
        ensure_type("430", AccountType.ASSET, BalanceSide.DEBIT)
        ensure_type("470", AccountType.ASSET, BalanceSide.DEBIT)
        ensure_type("475", AccountType.LIABILITY, BalanceSide.CREDIT)
        
        # Test Group 5
        ensure_type("570", AccountType.ASSET, BalanceSide.DEBIT)
        ensure_type("520", AccountType.LIABILITY, BalanceSide.CREDIT)
        
        # Test Group 6
        ensure_type("600", AccountType.EXPENSE, BalanceSide.DEBIT)
        
        # Test Group 7
        ensure_type("700", AccountType.INCOME, BalanceSide.CREDIT)

    def test_install_chart(self):
        from accounting.services.chart_installer import install_chart_standard
        
        # Create Template Structure
        # 1 -> 10 -> 100
        g1 = TemplateAccount.objects.create(template=self.template, code="1", name="Basic Financing")
        g10 = TemplateAccount.objects.create(template=self.template, code="10", name="Capital", parent=g1)
        a100 = TemplateAccount.objects.create(template=self.template, code="100", name="Social Capital", parent=g10)
        
        # 4 -> 43 -> 430
        g4 = TemplateAccount.objects.create(template=self.template, code="4", name="Creditors/Debtors")
        g43 = TemplateAccount.objects.create(template=self.template, code="43", name="Clients", parent=g4)
        a430 = TemplateAccount.objects.create(template=self.template, code="430", name="Clients", parent=g43)

        # Run Install
        count = install_chart_standard(self.company, self.template)
        
        # Verify
        self.assertEqual(count, 6)
        
        # Check Hierarchy
        c_100 = CompanyAccount.objects.get(company=self.company, code="100")
        self.assertEqual(c_100.parent.code, "10")
        self.assertEqual(c_100.parent.parent.code, "1")
        self.assertIsNone(c_100.parent.parent.parent)
        
        # Check Attributes
        self.assertEqual(c_100.account_type, AccountType.EQUITY)
        self.assertEqual(c_100.normal_balance, BalanceSide.CREDIT)
        
        c_430 = CompanyAccount.objects.get(company=self.company, code="430")
        self.assertEqual(c_430.account_type, AccountType.ASSET)
        self.assertEqual(c_430.normal_balance, BalanceSide.DEBIT)


