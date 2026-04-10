import logging
from django.db import transaction
from invoices.models import Invoice
from accounting.models import Journal, JournalEntry, JournalEntryLine, CompanyAccount, EntryStatus,Journal,JournalType

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

        # B. Taxes (VAT, IRPF, etc.)
        for tax in invoice.get_taxes():
            # Skip taxes with zero amount to avoid DB constraint violations
            if not tax.calculated_amount or tax.calculated_amount.amount <= 0:
                continue
                
            if tax.tax_type.is_debit:
                # Typically VAT (472)
                account = self._resolve_tax_account(tax.tax_type, is_purchase=True)
                JournalEntryLine.objects.create(
                    company=self.company,
                    entry=entry,
                    account=account,
                    description=f"{tax.tax_type.tax_name} {tax.selected_percentage}% - {invoice.invoice_number}",
                    debit=tax.calculated_amount,
                    credit=0
                )
            else:
                # Typically IRPF Retention (4751) - Journalized as Credit in Purchases
                account = self._resolve_tax_account(tax.tax_type, is_purchase=True)
                JournalEntryLine.objects.create(
                    company=self.company,
                    entry=entry,
                    account=account,
                    description=f"{tax.tax_type.tax_name} {tax.selected_percentage}% - {invoice.invoice_number}",
                    debit=0,
                    credit=tax.calculated_amount
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
        journal, _ = Journal.objects.get_or_create(
            company=self.company,
            type=JournalType.PURCHASE,
            defaults={'name': 'Compras', 'code': 'COM'}
        )
        return journal

    def _resolve_tax_account(self, tax_type, is_purchase=True):
        """
        Resolves a TaxType to a specific CompanyAccount.
        1. Try to use manual mapping from TaxType.purchase_account/sales_account (TemplateAccount)
        2. Resolve TemplateAccount to CompanyAccount for this company
        3. Fallback to historical 'starts with' logic.
        """
        template_acc = tax_type.purchase_account if is_purchase else tax_type.sales_account
        
        if template_acc:
            account = CompanyAccount.objects.filter(
                company=self.company,
                template_account=template_acc,
                is_postable=True
            ).first()
            if account:
                return account
        
        # Fallback to historical logic
        if is_purchase:
            if tax_type.is_debit:
                return self._get_vat_account()
            else:
                return self._get_irpf_account()
        
        # Sales fallback (to be expanded)
        return None

    def _get_vat_account(self):
        # Find account starting with 472 (Hacienda Pública, IVA Soportado)
        account = CompanyAccount.objects.filter(
            company=self.company,
            code__startswith='472',
            is_postable=True
        ).first()
        if not account:
            account = CompanyAccount.objects.filter(company=self.company, is_tax_account=True, code__startswith='472').first()
        return account

    def _get_irpf_account(self):
        account = CompanyAccount.objects.filter(
            company=self.company,
            code__startswith='4751',
            is_postable=True
        ).first()
        if not account:
            account = CompanyAccount.objects.filter(company=self.company, is_tax_account=True, code__startswith='475').first()
        return account

    def _get_provider_account(self, provider):
        if not provider:
            return None
            
        if getattr(provider, 'account', None):
            return provider.account
            
        # Get root from company chart or fallback to 400
        from accounting.models import CompanyChartOfAccounts
        chart = getattr(self.company, 'chart', None)
        root = chart.provider_account_root if chart else "400"
        
        provider_code = f"{root}{provider.id:04d}"
        
        account = CompanyAccount.objects.filter(
            company=self.company,
            code=provider_code
        ).first()
        
        if not account:
            parent = CompanyAccount.objects.filter(company=self.company, code=root).first()
            
            account = CompanyAccount.objects.create(
                company=self.company,
                code=provider_code,
                name=provider.name,
                parent=parent,
            )

            provider.account = account
            provider.save(update_fields=['account'])
            logger.info(f"Automatically created accounting account {provider_code} for provider {provider.name}")

        return account
