from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils.translation import gettext as _

from accounting.models import AccountRole, CompanyAccount, CompanyChartOfAccounts, EntryStatus, JournalEntry, JournalType
from accounting.services.journal_generator import (
    LEDGER_CURRENCY, JournalizeError, MissingAccount, _JournalGeneratorBase,
)
from accounting.services.payment_journal.configured import configured_strategy
from accounting.services.payment_journal.registry import strategy_for, treasury_strategy_for
from payments.models import Payment, PaymentDirection, PaymentMethod, PaymentStatus

if TYPE_CHECKING:
    from materials.models import Provider
    from sales.models import Client

logger = logging.getLogger(__name__)

# Payments the ledger books; any other status takes their entry back out
BOOKED_STATUSES = (PaymentStatus.CONFIRMED, PaymentStatus.RECONCILED)


class NoStrategy(JournalizeError):
    pass


class PaymentJournalService(_JournalGeneratorBase):
    """
    Journal entries for payments (PGC), one live entry per payment:

    - the treasury: the payment's bank account (its own account under the
      chart's bank root, 572xxxx, opened on first use), or the CASH role for
      cash, or for a method moving no money what its TreasuryStrategy says
      (registry.py). Debit when money comes in, credit when it goes out
    - per allocation, the counterpart its document's strategy books (see
      registry.py): a sales invoice's client, a purchase invoice's provider...
    - a document type without a strategy in code books to the account set
      on the allocation, else to the company's AllocationAccountRule for it;
      with neither, the payment isn't booked (the setup page lists it)
    - whatever isn't allocated goes to the account set on the payment, else
      stays on the client's (or provider's) own account, so applying it
      later changes nothing in the ledger, else to its bank transaction
      category's account (bank fees, interest...)

    When the payment changes (amount, date, allocations, status) its posted
    entry is reversed and booked again in full, so every edit shows in the
    ledger; a draft is just rebuilt. A payment no longer confirmed is only
    reversed. Payments dated before the chart's ledger start date are left
    out: the opening entry carries their balances.
    """

    NoStrategy = NoStrategy

    @staticmethod
    def schedule(payment_id):
        """
        Journalize the payment once the current transaction commits, once
        however many times it was saved or allocated in it. Never raises.
        """
        connection = transaction.get_connection()
        if any(getattr(callback, "payment_journal_id", None) == payment_id
               for _sids, callback, *_rest in connection.run_on_commit):
            return

        def journalize():
            PaymentJournalService.journalize_quietly(payment_id)

        journalize.payment_journal_id = payment_id
        transaction.on_commit(journalize)

    @staticmethod
    def journalize_quietly(payment_id):
        """sync() for a payment that may be gone; what fails is logged, never raised."""
        payment = Payment.objects.select_related("company").filter(pk=payment_id).first()
        # A company without a chart keeps no ledger
        if not payment or not CompanyChartOfAccounts.objects.filter(company_id=payment.company_id).exists():
            return None
        try:
            return PaymentJournalService(payment.company).sync(payment)
        except JournalizeError as e:
            logger.warning("Payment %s not journalized: %s", payment_id, e)
        except Exception:
            logger.exception("Could not journalize payment %s", payment_id)
        return None

    def sync(self, payment):
        """
        Bring the ledger in line with the payment. Returns the new entry, or
        None when the ledger already matched or there's nothing to book.
        Raises JournalizeError, leaving the ledger as it was.
        """
        with transaction.atomic():
            payment = (
                Payment.objects.select_for_update(of=("self",))
                .select_related("bank_account", "client", "provider", "account")
                .get(pk=payment.pk)
            )
            # Older payments are in the opening entry's balances; the ledger isn't touched for them
            if self._before_start(payment.payment_date):
                return None
            amounts = self._signed_amounts(payment) if payment.status in BOOKED_STATUSES else []

            live = payment.journal_entries.filter(reversal_of__isnull=True, reversal__isnull=True)
            live.filter(status=EntryStatus.DRAFT).delete()
            posted = live.filter(status=EntryStatus.POSTED).first()
            if posted and posted.date == payment.payment_date and self._totals_of(posted) == self._totals(amounts):
                return None
            if posted:
                self._reverse_entry(posted)
            if not amounts:
                return None

            entry = JournalEntry.objects.create(
                company=self.company,
                journal=self._get_treasury_journal(payment),
                date=payment.payment_date,
                reference=self._reference(payment),
                status=EntryStatus.DRAFT,
                payment=payment,
            )
            self._create_lines(entry, amounts, LEDGER_CURRENCY)
        self._try_post(entry)
        return entry

    # -- accounts strategies ask for (the registry's Ledger) ------------------

    def client_account(self, client: Client) -> CompanyAccount:
        return self._get_partner_account(client, "client_account_root")

    def provider_account(self, provider: Provider) -> CompanyAccount:
        return self._get_partner_account(provider, "provider_account_root")

    def role_account(self, role: AccountRole) -> CompanyAccount:
        return self._role_account(role)

    # -- building -------------------------------------------------------------

    def _signed_amounts(self, payment):
        """[(account, description, amount)], debit positive."""
        self._check_currency(
            payment.amount_currency,
            _("Payments in %(currency)s can't be journalized yet: the ledger is kept in %(ledger)s."),
        )
        total = Decimal(str(payment.amount.amount))
        if not total:
            return []
        treasury = (self._treasury_account(payment), self._reference(payment), self._sign(payment) * total)
        return [treasury] + self.counterpart_amounts(payment)

    def counterpart_amounts(self, payment, only=None):
        """
        What the payment settled, opposite the bank: [(account, description,
        amount)], debit positive. ``only`` (models) leaves allocations to any
        other document out, though they still count as allocated.
        """
        sign = self._sign(payment)
        amounts = []
        allocated = Decimal("0")
        for allocation in payment.allocations.filter(is_mirror=False).select_related("content_type").order_by("pk"):
            applied = Decimal(str(allocation.amount_applied.amount))
            allocated += applied
            if only and allocation.content_type.model_class() not in only:
                continue
            lines = self._allocation_lines(allocation)
            if sum(line.amount for line in lines) != applied:
                raise JournalizeError(
                    _("The lines booked for %(document)s don't add up to the %(amount)s allocated to it.")
                    % {"document": allocation.related_object, "amount": allocation.amount_applied}
                )
            amounts += [(line.account, line.description, -sign * line.amount) for line in lines]

        remainder = Decimal(str(payment.amount.amount)) - allocated
        if remainder:
            account, description = self._unallocated_account(payment)
            amounts.append((account, description, -sign * remainder))
        return amounts

    @staticmethod
    def _sign(payment):
        """Money in is a debit to the bank; everything else takes the other side."""
        return 1 if payment.direction == PaymentDirection.INBOUND else -1

    def _allocation_lines(self, allocation):
        """By the document type's strategy in code, else by the account configured in the admin."""
        model = allocation.content_type.model_class()
        strategy = strategy_for(model) if model else None
        if not strategy and configured_strategy.account_for(allocation, self.company):
            strategy = configured_strategy
        if not strategy:
            raise NoStrategy(
                _("Payments allocated to %(documents)s can't be journalized: add a payment account rule "
                  "for them, or set an account on the allocation.")
                % {"documents": model._meta.verbose_name_plural if model else allocation.content_type}
            )
        document = allocation.related_object
        if document is None:
            raise JournalizeError(
                _("An allocation points to %(type)s #%(id)s, which no longer exists.")
                % {"type": model._meta.verbose_name, "id": allocation.object_id}
            )
        return strategy.lines(allocation, document, self)

    def _treasury_account(self, payment):
        # A method moving no money (paid by another company) says where it's booked instead
        treasury = treasury_strategy_for(payment.method)
        if treasury:
            return treasury.account(payment, self)
        if payment.method == PaymentMethod.CASH:
            return self._role_account(AccountRole.CASH)
        if payment.bank_account_id:
            bank = payment.bank_account
            digits = (bank.iban or bank.account_number or "")[-4:]
            return self._get_partner_account(bank, "bank_account_root", name=f"{bank.bank_name} {digits}".strip())
        raise MissingAccount(_("The payment has no bank account: set the one the money went through."))

    def _unallocated_account(self, payment):
        """
        (account, description) for what isn't allocated: the account set on
        the payment, else the payer's or payee's own, else the account of
        the category its bank transaction was reconciled under.
        """
        from banking.models import BankReconciliation

        if payment.account_id:
            return payment.account, payment.reference or payment.account.name
        partners = [(payment.client, "client_account_root"), (payment.provider, "provider_account_root")]
        if payment.direction == PaymentDirection.OUTBOUND:
            partners.reverse()
        for partner, root_field in partners:
            if partner:
                return self._get_partner_account(partner, root_field), f"Entrega a cuenta - {partner.name}"
        reconciliation = (
            BankReconciliation.objects.filter(payment=payment)
            .select_related("bank_transaction__category__account").first()
        )
        category = reconciliation.bank_transaction.category if reconciliation else None
        if category and category.account_id:
            return category.account, f"{category.name} - {payment.reference or payment.pk}"
        raise MissingAccount(
            _("Part of the payment isn't allocated and nothing says where to book it: set a client or provider, "
              "an account on the payment, or one on its bank transaction category.")
        )

    @staticmethod
    def _totals(amounts):
        totals = defaultdict(Decimal)
        for account, _description, amount in amounts:
            totals[account.pk] += amount
        return {account_id: amount for account_id, amount in totals.items() if amount}

    @staticmethod
    def _totals_of(entry):
        totals = defaultdict(Decimal)
        for line in entry.lines.all():
            totals[line.account_id] += line.debit.amount - line.credit.amount
        return {account_id: amount for account_id, amount in totals.items() if amount}

    @staticmethod
    def _reference(payment):
        kind = "Cobro" if payment.direction == PaymentDirection.INBOUND else "Pago"
        return f"{kind} {payment.reference or payment.pk}"

    def _get_treasury_journal(self, payment):
        treasury = treasury_strategy_for(payment.method)
        if treasury:
            return self._get_journal(*treasury.journal)
        if payment.method == PaymentMethod.CASH:
            return self._get_journal(JournalType.CASH, "Caja", "CAJ")
        return self._get_journal(JournalType.BANK, "Bancos", "BAN")
