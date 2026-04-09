import logging
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from invoices.models import Invoice, AppliedTax
from accounting.models import Journal, JournalEntry, JournalEntryLine, CompanyAccount, EntryStatus

logger = logging.getLogger(__name__)

class JournalGeneratorService:
    """
    Service to generate Journal Entries mapping to the PGC (Plan General Contable).
    Targeted specifically at Purchase Invoices (Invoices).
    """

    def __init__(self, company):
        self.company = company

    def journalize_all_pending(self):
        """
        Processes all invoices that are fully classified but don't have a journal entry yet.
        """
        # Simplified query using the new ForeignKey relationship
        invoices = Invoice.objects.filter(
            company=self.company,
            ocr_processed=True,
            needs_manual_review=False,
            journal_entries__isnull=True
        )
        
        processed_count = 0
        for invoice in invoices:
            try:
                if self.journalize_invoice(invoice):
                    processed_count += 1
            except Exception as e:
                logger.error(f"Failed to journalize invoice {invoice.id}: {str(e)}")
        
        return processed_count

    @transaction.atomic
    def journalize_invoice(self, invoice: Invoice):
        """
        Creates a JournalEntry for a specific purchase invoice.
        
        Lines:
        - DEBIT: Expense accounts (from InvoiceItem.account)
        - DEBIT: VAT Input account (typically 472*)
        - CREDIT: Provider account (typically 400*)
        """
        if not invoice.date_issued:
            logger.warning(f"Invoice {invoice.id} has no date_issued, skipping journalization.")
            return None

        # 1. Gather accounts
        journal = self._get_purchase_journal()
        provider_account = self._get_provider_account(invoice.provider)
        vat_account = self._get_vat_account()

        if not provider_account:
            logger.warning(f"Could not find provider account for {invoice.provider}, skipping.")
            return None

        # 1.5 Safety check: Skip if no total
        if not invoice.total_amount or invoice.total_amount.amount <= 0:
            logger.warning(f"Invoice {invoice.id} has no valid total, skipping journalization.")
            return None

        # 2. Create Journal Entry
        entry = JournalEntry.objects.create(
            company=self.company,
            journal=journal,
            date=invoice.date_issued,
            reference=f"INV {invoice.invoice_number if invoice.invoice_number else invoice.id}",
            status=EntryStatus.DRAFT,
            purchase_invoice=invoice  # Set the direct link
        )

        # 3. Create Lines
        
        # A. Debit Expenses (from items)
        for item in invoice.items.all():
            if not item.account:
                # If an item is not classified, we can't fully journalize
                # Use a default or fall back?
                continue
            
            # Skip items with zero total to avoid DB constraint violations
            if not item.line_total or item.line_total.amount <= 0:
                continue
                
            JournalEntryLine.objects.create(
                company=self.company,
                entry=entry,
                account=item.account,
                description=f"{invoice.provider.name}: {item.description}",
                debit=item.line_total,
                credit=0
            )

        # B. Debit Taxes (VAT, etc.)
        for tax in invoice.get_taxes():
            if tax.tax_type.is_debit:
                # Skip taxes with zero amount to avoid DB constraint violations
                if not tax.calculated_amount or tax.calculated_amount.amount <= 0:
                    continue
                JournalEntryLine.objects.create(
                    company=self.company,
                    entry=entry,
                    account=vat_account,
                    description=f"{tax.tax_type.tax_name} {tax.selected_percentage}% - {invoice.invoice_number}",
                    debit=tax.calculated_amount,
                    credit=0
                )

        # C. Credit Provider
        JournalEntryLine.objects.create(
            company=self.company,
            entry=entry,
            account=provider_account,
            description=f"Factura {invoice.invoice_number} - {invoice.provider.name}",
            debit=0,
            credit=invoice.total_amount
        )

        # 4. Attempt to post if balanced
        try:
            if entry.is_balanced():
                # entry.post() # Stay as Draft for now to allow user review? 
                # User asked for "daily task to do all entries". Usually entries are posted.
                # However, post() requires numbering logic. I'll leave them in POSTED if they balance.
                entry.post()
        except Exception as e:
            logger.error(f"Failed to post entry for invoice {invoice.id}: {str(e)}")

        return entry

    def _get_purchase_journal(self):
        from accounting.models import Journal, JournalType
        journal, _ = Journal.objects.get_or_create(
            company=self.company,
            type=JournalType.PURCHASE,
            defaults={'name': 'Compras', 'code': 'COM'}
        )
        return journal

    def _get_vat_account(self):
        # Find account starting with 472 (Hacienda Pública, IVA Soportado)
        account = CompanyAccount.objects.filter(
            company=self.company,
            code__startswith='472',
            is_postable=True
        ).first()
        if not account:
            # Fallback or create? Usually it should exist in the PGC
            account = CompanyAccount.objects.filter(company=self.company, is_tax_account=True, code__startswith='47').first()
        return account

    def _get_provider_account(self, provider):
        if not provider:
            return None
            
        # 1. Prioritize explicitly assigned account on the Provider model
        if getattr(provider, 'account', None):
            return provider.account
            
        # 2. Automation: Find or Create a specific account for this provider
        # We use code 400 + provider ID (padded to 4 digits, e.g., 4000001)
        provider_code = f"400{provider.id:04d}"
        
        account = CompanyAccount.objects.filter(
            company=self.company,
            code=provider_code
        ).first()
        
        if not account:
            # Create the account
            # Try to find parent '400' for hierarchy
            parent = CompanyAccount.objects.filter(company=self.company, code='400').first()
            
            account = CompanyAccount.objects.create(
                company=self.company,
                code=provider_code,
                name=provider.name,
                parent=parent,
            )
            # Link it back to the Provider model for future use
            provider.account = account
            provider.save(update_fields=['account'])
            logger.info(f"Automatically created accounting account {provider_code} for provider {provider.name}")

        return account
