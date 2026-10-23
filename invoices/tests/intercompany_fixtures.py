"""
Shared fixtures for the intercompany payment tests.

``IntercompanyMixin``: two group companies with a ledger each (a chart and
an open fiscal year), the debtor's provider invoice of 1,210.00 and the
payer's bank account; ``register()`` pays the invoice from the payer and
runs what is left for the commit (the payments' journal entries), the way
a request would.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from djmoney.money import Money

from accounting.models import JournalEntryLine
from invoices.services.intercompany_payment_service import IntercompanyPaymentService
from invoices.tests.factories import (
    make_bank_account, make_company, make_ledger, make_provider, make_purchase_invoice,
)
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod, PaymentStatus

PAID_ON = date(2026, 10, 5)


class IntercompanyMixin:
    def setUp(self):
        super().setUp()
        self.debtor = make_company(name="Aluminios A", legal_name="Aluminios A SL", tax_identifier="B11111111")
        self.payer = make_company(name="Grupo B", legal_name="Grupo B SL", tax_identifier="B22222222")
        make_ledger(company=self.debtor)
        make_ledger(company=self.payer)
        self.provider = make_provider(company=self.debtor, name="Perfiles SA", tax_identifier="B33333333")
        self.invoice = make_purchase_invoice(company=self.debtor, provider=self.provider, total="1210.00",
                                             number="F-77")
        self.payer_bank = make_bank_account(company=self.payer, bank_name="Banco B", iban="ES0000000000000000002222")
        self.user = User.objects.create_superuser("ic", "ic@example.com", "pw")

    def register(self, amount="1210.00", invoice=None, **fields):
        fields.setdefault("payer_company", self.payer)
        fields.setdefault("bank_account", self.payer_bank)
        fields.setdefault("payment_date", PAID_ON)
        with self.captureOnCommitCallbacks(execute=True):
            return IntercompanyPaymentService.create(
                user=self.user, source_invoice=invoice or self.invoice, amount=Decimal(amount), **fields,
            )

    def pay(self, company, document, amount, direction, bank_account):
        """A bank payment of ``company`` allocated to ``document`` in full, journalized."""
        with self.captureOnCommitCallbacks(execute=True):
            payment = Payment.objects.create(
                company=company, payment_date=PAID_ON, amount=Money(amount, "EUR"), direction=direction,
                method=PaymentMethod.TRANSFER, status=PaymentStatus.CONFIRMED, bank_account=bank_account,
            )
            PaymentAllocation.objects.create(
                payment=payment, content_type=ContentType.objects.get_for_model(document), object_id=document.pk,
                amount_applied=Money(amount, "EUR"),
            )
        return payment

    def reimburse(self, record, debtor_bank):
        """The debtor pays the payer back: its bank payment of the debt, the payer's receipt of the debit note."""
        amount = record.amount.amount
        self.pay(self.debtor, record.mirror_invoice, amount, PaymentDirection.OUTBOUND, debtor_bank)
        self.pay(self.payer, record.debit_note, amount, PaymentDirection.INBOUND, self.payer_bank)

    @staticmethod
    def balance(account):
        """Debit minus credit of ``account`` over every entry, reversals included."""
        lines = JournalEntryLine.objects.filter(account=account)
        return sum((line.debit.amount - line.credit.amount for line in lines), Decimal("0"))
