import logging
import re
from collections import defaultdict
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from djmoney.money import Money

from invoices.models import AppliedTax, Invoice, InvoiceDocumentType, SalesInvoice, SalesInvoiceStatus
from accounting.models import (
    AccountRole, BusinessUnit, CompanyAccount, CompanyChartOfAccounts, EntryStatus, IncomeType, Journal, JournalEntry,
    JournalEntryLine, JournalType,
)

logger = logging.getLogger(__name__)

# The ledger is kept in euros; invoices in any other currency are refused
# until amounts are converted
LEDGER_CURRENCY = "EUR"


class JournalizeError(Exception):
    """An invoice that can't be journalized as configured; the message says what to fix."""


class MissingAccount(JournalizeError):
    pass


class MissingBusinessUnit(MissingAccount):
    pass


class UnsupportedCurrency(JournalizeError):
    pass


class _JournalGeneratorBase:
    """Lookups shared by the purchase and sales generators."""

    JournalizeError = JournalizeError
    MissingAccount = MissingAccount
    MissingBusinessUnit = MissingBusinessUnit
    UnsupportedCurrency = UnsupportedCurrency

    @staticmethod
    def _check_currency(currency, message=None):
        """``message`` replaces the invoices' one; it takes %(currency)s and %(ledger)s."""
        currency = str(currency or LEDGER_CURRENCY)
        if currency != LEDGER_CURRENCY:
            message = message or _("Invoices in %(currency)s can't be journalized yet: the ledger is kept in %(ledger)s.")
            raise UnsupportedCurrency(message % {"currency": currency, "ledger": LEDGER_CURRENCY})

    def __init__(self, company):
        self.company = company

    def _get_journal(self, journal_type, name, code):
        journal, _created = Journal.objects.get_or_create(
            company=self.company,
            type=journal_type,
            defaults={'name': name, 'code': code}
        )
        return journal

    def _get_partner_account(self, partner, root_field, name=None):
        """
        The provider's, client's or bank account's own account, created on
        first use and stored back on the partner. Its code is the chart's
        root for that kind of partner (``root_field``) followed by the next
        consecutive number in the company, padded to the chart's account
        length (e.g. 4300001, 4300002, ...). ``name`` defaults to the
        partner's.
        """
        if not partner:
            return None
        # Two invoices of a new partner at once must not open two accounts
        partner = type(partner).objects.select_for_update().get(pk=partner.pk)
        if partner.account_id:
            return partner.account

        # Locking the chart serializes numbering within the company
        chart = CompanyChartOfAccounts.objects.select_for_update().filter(company=self.company).first()
        default = CompanyChartOfAccounts._meta.get_field
        root = getattr(chart, root_field) if chart else default(root_field).default
        length = chart.partner_account_length if chart else default("partner_account_length").default
        code = self._next_partner_code(root, length)

        account = CompanyAccount.objects.create(
            company=self.company,
            code=code,
            name=name or partner.name,
            parent=CompanyAccount.objects.filter(company=self.company, code=root).first(),
        )
        logger.info(f"Automatically created accounting account {code} for {account.name}")

        partner.account = account
        partner.save(update_fields=['account'])
        return account

    def _next_partner_code(self, root, length):
        width = length - len(root)
        codes = CompanyAccount.objects.filter(
            company=self.company, code__regex=rf"^{re.escape(root)}[0-9]{{{width}}}$",
        ).values_list("code", flat=True)
        number = max((int(code[len(root):]) for code in codes), default=0) + 1
        if len(str(number)) > width:
            raise ValueError(f"No {length}-digit account codes left under {root} in {self.company}")
        return f"{root}{number:0{width}d}"

    def _chart(self):
        return CompanyChartOfAccounts.objects.filter(company=self.company).first()

    def _before_start(self, day):
        """Dated before the ledger starts: its balance comes in with the opening entry instead."""
        start = (
            CompanyChartOfAccounts.objects.filter(company=self.company)
            .values_list("ledger_start_date", flat=True).first()
        )
        return bool(start and day and day < start)

    def _role_account(self, role):
        account = CompanyAccount.for_role(self.company.pk, role)
        if not account:
            raise MissingAccount(
                _("No account has the role “%(role)s”: assign it in the company's accounts.")
                % {"role": AccountRole(role).label}
            )
        return account

    def _resolve_tax_account(self, tax_type, is_purchase=True):
        """
        The company's account for a tax: the template account mapped on the
        tax type (purchase or sales side), as installed in this company.
        Raises MissingAccount when the tax isn't mapped or the company lacks it.
        """
        template_acc = tax_type.purchase_account if is_purchase else tax_type.sales_account
        side = _("purchases") if is_purchase else _("sales")
        if not template_acc:
            raise MissingAccount(
                _("Tax %(tax)s has no account for %(side)s: set it on the tax type.")
                % {"tax": tax_type.tax_name, "side": side}
            )
        account = CompanyAccount.objects.filter(
            company=self.company,
            template_account=template_acc,
            is_postable=True,
            is_active=True,
        ).order_by("code").first()
        if not account:
            raise MissingAccount(
                _("Tax %(tax)s books %(side)s to %(code)s, which this company doesn't have: install the chart.")
                % {"tax": tax_type.tax_name, "side": side, "code": template_acc.code}
            )
        return account

    @staticmethod
    def _try_post(entry):
        """
        Post a balanced entry; one the ledger refuses (no fiscal year, lock
        date) stays DRAFT, the reason kept on ``entry.post_error``.
        """
        entry.post_error = None
        if not entry.is_balanced():
            entry.post_error = _("Debits and credits don't match.")
            return
        try:
            entry.post()
        except ValidationError as e:
            entry.post_error = " ".join(e.messages)
            logger.warning("Journal entry %s left as draft: %s", entry.pk, entry.post_error)

    def _create_lines(self, entry, amounts, currency):
        """``amounts``: [(account, description, amount)], debit positive; zeros are skipped."""
        JournalEntryLine.objects.bulk_create([
            JournalEntryLine(
                company=self.company,
                entry=entry,
                account=account,
                description=description,
                debit=Money(max(amount, 0), currency),
                credit=Money(max(-amount, 0), currency),
            )
            for account, description, amount in amounts
            if amount
        ])

    def _reverse_entry(self, entry, date=None):
        """
        A posted entry's reversing entry (debits and credits swapped), linked
        to the same documents, posted when the ledger allows it.
        """
        with transaction.atomic():
            reversal = JournalEntry.objects.create(
                company=self.company,
                journal=entry.journal,
                date=date or timezone.localdate(),
                reference=f"Anulación {entry.reference}",
                status=EntryStatus.DRAFT,
                purchase_invoice_id=entry.purchase_invoice_id,
                sales_invoice_id=entry.sales_invoice_id,
                payment_id=entry.payment_id,
                reversal_of=entry,
            )
            JournalEntryLine.objects.bulk_create([
                JournalEntryLine(
                    company=self.company,
                    entry=reversal,
                    account_id=line.account_id,
                    description=line.description,
                    debit=line.credit,
                    credit=line.debit,
                )
                for line in entry.lines.all()
            ])
        self._try_post(reversal)
        return reversal


class JournalGeneratorService(_JournalGeneratorBase):
    """
    Service to generate Journal Entries mapping to the PGC (Plan General Contable).
    Targeted specifically at Purchase Invoices (Invoices).
    """

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
        ).exclude(document_type=InvoiceDocumentType.INTERCOMPANY)
        chart = self._chart()
        if chart and chart.ledger_start_date:
            invoices = invoices.filter(date_issued__gte=chart.ledger_start_date)
        
        processed_count = 0
        unbalanced_count = 0
        errors = []
        for invoice in invoices:
            try:
                entry = self.journalize_invoice(invoice)
                if entry:
                    if entry.status == EntryStatus.POSTED:
                        processed_count += 1
                    else:
                        unbalanced_count += 1
            except Exception as e:
                err_msg = f"Invoice {invoice.invoice_number or invoice.id}: {str(e)}"
                errors.append(err_msg)
        
        return {
            "processed": processed_count,
            "unbalanced": unbalanced_count,
            "errors": errors
        }

    @transaction.atomic
    def journalize_invoice(self, invoice: Invoice):
        """
        Creates a JournalEntry for a specific purchase invoice.
        
        Lines:
        - DEBIT: Expense accounts (from InvoiceItem.account)
        - DEBIT: VAT Input account (typically 472*)
        - CREDIT: Provider account (typically 400*)

        A debt to a group company that paid for us has no entry of its own:
        the settlement of the invoice it paid booked it.
        """
        if invoice.is_intercompany:
            return None
        if not invoice.date_issued:
            logger.warning(f"Invoice {invoice.id} has no date_issued, skipping journalization.")
            return None
        if self._before_start(invoice.date_issued):
            return None
        self._check_currency(invoice.currency)

        # 1. Gather accounts
        journal = self._get_purchase_journal()
        provider_account = self._get_provider_account(invoice.provider)

        if not provider_account:
            logger.warning(f"Could not find provider account for {invoice.provider}, skipping.")
            return None

        # 1.5 Safety check: Skip if no total (allow negative for returns)
        if not invoice.total_amount or invoice.total_amount.amount == 0:
            logger.warning(f"Invoice {invoice.id} has no valid total (0.00), skipping journalization.")
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
            # Zero lines (free shipping, kg notes) carry nothing and break check_debit_xor_credit
            if not item.line_total or item.line_total.amount == 0:
                continue

            # Support returns: positive line_total -> Debit, negative line_total -> Credit
            debit_amt = item.line_total if item.line_total.amount > 0 else 0
            credit_amt = abs(item.line_total) if item.line_total.amount < 0 else 0

            JournalEntryLine.objects.create(
                company=self.company,
                entry=entry,
                account=item.account,
                description=f"{invoice.provider.name}: {item.description}",
                debit=debit_amt,
                credit=credit_amt
            )

        # B. Taxes (VAT, IRPF, etc.)
        for tax in invoice.get_taxes():
            if not tax.calculated_amount:
                continue

            tax_amount = abs(tax.calculated_amount.amount)
            
            # Skip taxes with zero amount to avoid DB constraint violations
            if tax_amount <= 0:
                continue
            
            # Use absolute value for the money object to ensure it passes DB constraints (must be positive)
            money_amount = tax.calculated_amount.__class__(tax_amount, tax.calculated_amount.currency)

            # Support returns: 
            # If is_debit=True (VAT): positive -> Debit, negative -> Credit
            # If is_debit=False (IRPF): positive -> Credit, negative -> Debit
            
            is_pos = tax.calculated_amount.amount > 0
            
            if tax.tax_type.is_debit:
                # Typically VAT
                account = self._resolve_tax_account(tax.tax_type, is_purchase=True)
                JournalEntryLine.objects.create(
                    company=self.company,
                    entry=entry,
                    account=account,
                    description=f"{tax.tax_type.tax_name} {tax.selected_percentage}% - {invoice.invoice_number}",
                    debit=money_amount if is_pos else 0,
                    credit=0 if is_pos else money_amount
                )
            else:
                # Typically IRPF Retention
                account = self._resolve_tax_account(tax.tax_type, is_purchase=True)
                JournalEntryLine.objects.create(
                    company=self.company,
                    entry=entry,
                    account=account,
                    description=f"{tax.tax_type.tax_name} {tax.selected_percentage}% - {invoice.invoice_number}",
                    debit=0 if is_pos else money_amount,
                    credit=money_amount if is_pos else 0
                )

        # Support returns: positive total -> Credit, negative total -> Debit
        debit_provider = abs(invoice.total_amount) if invoice.total_amount.amount < 0 else 0
        credit_provider = invoice.total_amount if invoice.total_amount.amount > 0 else 0

        JournalEntryLine.objects.create(
            company=self.company,
            entry=entry,
            account=provider_account,
            description=f"Factura {invoice.invoice_number} - {invoice.provider.name}",
            debit=debit_provider,
            credit=credit_provider
        )

        # 4. Attempt to post if balanced
        if entry.is_balanced():
            entry.post()

        return entry

    def _get_purchase_journal(self):
        return self._get_journal(JournalType.PURCHASE, 'Compras', 'COM')

    def _get_provider_account(self, provider):
        return self._get_partner_account(provider, "provider_account_root")


class SalesJournalGeneratorService(_JournalGeneratorBase):
    """
    Journal entries for issued sales invoices (PGC):

    - DEBIT  client (the chart's client root + next number): total_amount
    - CREDIT sales of goods / of services (roles GOODS_SALES, SERVICES_SALES):
      each line by its business unit's income type (the company's default unit
      for a line without one). Advances are ordinary sales; a deduction line
      (negative) follows its advance line's unit, so it takes back what the
      advance booked
    - DEBIT  sales discounts (role SALES_DISCOUNTS): the header discount, as
      printed on the invoice
    - CREDIT output VAT (477) / DEBIT IRPF withheld by the client (473)
    - rounding role: whatever cents the stored total differs by, within the
      chart's tolerance (imported invoices keep the total typed in)

    Amounts are signed debit-positive while building; a line's side is its
    sign, so a negative invoice flips every side.
    """

    INCOME_ROLES = {
        IncomeType.GOODS: AccountRole.GOODS_SALES,
        IncomeType.SERVICES: AccountRole.SERVICES_SALES,
    }

    def journalize_invoice(self, invoice: SalesInvoice):
        """
        The invoice's entry, posted when balanced. None if it already has one,
        and for a debit note: what it charges is no income, the payment it
        charges for already booked the debt.
        """
        if invoice.status != SalesInvoiceStatus.FINAL or invoice.is_intercompany:
            return None
        if invoice.journal_entries.filter(reversal_of__isnull=True).exists():
            return None
        if self._before_start(invoice.date_issued):
            return None

        self._check_currency(invoice.currency)
        with transaction.atomic():
            amounts = self._signed_amounts(invoice)
            entry = JournalEntry.objects.create(
                company=self.company,
                journal=self._get_sales_journal(),
                date=invoice.date_issued,
                reference=f"FV {invoice.invoice_number}",
                status=EntryStatus.DRAFT,
                sales_invoice=invoice,
            )
            self._create_lines(entry, amounts, invoice.currency)
        self._try_post(entry)
        return entry

    def retry_invoice(self, invoice: SalesInvoice):
        """
        Journalize again an issued invoice whose entry failed or stayed
        DRAFT: a draft is rebuilt from the invoice as it is now. Returns
        None when it already has a posted entry. Raises JournalizeError.
        """
        with transaction.atomic():
            invoice.journal_entries.filter(reversal_of__isnull=True, status=EntryStatus.DRAFT).delete()
            return self.journalize_invoice(invoice)

    def reverse_invoice_entries(self, invoice: SalesInvoice, date=None):
        """
        Undo a cancelled invoice's entry: a posted one gets a reversing entry
        (debits and credits swapped), a draft one is deleted. Returns the
        reversing entries created.
        """
        reversals = []
        for entry in invoice.journal_entries.filter(reversal_of__isnull=True, reversal__isnull=True):
            if entry.status == EntryStatus.DRAFT:
                entry.delete()
                continue
            if entry.status != EntryStatus.POSTED:
                continue
            reversals.append(self._reverse_entry(entry, date))
        return reversals

    def _signed_amounts(self, invoice):
        """[(account, description, amount)], debit positive."""
        number = invoice.invoice_number
        amounts = []

        # Client: the whole invoice
        chart = self._chart()
        client_account = self._get_partner_account(invoice.client, "client_account_root")
        total = Decimal(str(invoice.total_amount.amount))
        amounts.append((client_account, f"Factura {number} - {invoice.client.name}", total))

        # Income, by the lines' business units: goods or services (credit)
        by_role = defaultdict(Decimal)
        default_unit = BusinessUnit.default_for(self.company.pk)
        for item in invoice.items.select_related("business_unit", "advance_line__business_unit"):
            unit = (item.advance_line.business_unit if item.advance_line_id else item.business_unit) or default_unit
            if not unit:
                raise MissingBusinessUnit(
                    _("Line “%(line)s” has no business unit and the company has no default one.")
                    % {"line": item.description[:60]}
                )
            by_role[self.INCOME_ROLES[unit.income_type]] -= Decimal(str(item.line_total.amount))
        # The header discount, whole, on its own account (debit)
        discount = Decimal(str(invoice.discount_amount.amount)) if invoice.discount_amount else Decimal("0")
        if discount:
            by_role[AccountRole.SALES_DISCOUNTS] += discount
        for role, amount in by_role.items():
            amounts.append((self._role_account(role), f"Factura {number}", amount))

        # Invoice-level taxes (line-level ones only record an advance's VAT group)
        taxes = AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=invoice.pk,
        ).select_related("tax_type")
        for tax in taxes:
            if not tax.calculated_amount:
                continue
            account = self._resolve_tax_account(tax.tax_type, is_purchase=False)
            amount = Decimal(str(tax.calculated_amount.amount))
            signed = -amount if tax.tax_type.behavior == "add" else amount
            amounts.append((account, f"{tax.tax_type.tax_name} {tax.selected_percentage}% - {number}", signed))

        # Rounding: what's left, when small enough
        gap = -sum(amount for _, _, amount in amounts)
        rounding = CompanyAccount.for_role(self.company.pk, AccountRole.ROUNDING)
        if gap and rounding and chart and abs(gap) <= chart.rounding_tolerance:
            amounts.append((rounding, f"Redondeo - {number}", gap))

        return amounts

    def _get_sales_journal(self):
        return self._get_journal(JournalType.SALES, 'Ventas', 'VEN')
