"""Find the purchase invoice a direct debit charge (recibo) collects.

A provider paid by direct debit charges us on the invoice's due date, or on
the next bank business day when that falls on a weekend or a TARGET2 holiday
(SEPA settles on the TARGET2 calendar). A charge for exactly what one invoice
of the company still owes, on that day, is that invoice: no one needs to look
at it. The charge carries no IBAN and its payee name rarely matches the
provider's, so neither is required; the invoice's payment method is often
left as transfer, so it isn't either. Two candidates leave it to a human.

Most charges reach the bank before their invoice is registered, so the
invoice looks for its charge too (find_waiting_charge): same rule, from the
other side.
"""
from datetime import date, timedelta

from dateutil.easter import easter
from django.apps import apps

from payments.mixins import annotate_amount_remaining


def is_bank_business_day(day: date) -> bool:
    """Not a weekend nor a TARGET2 closing day."""
    if day.weekday() >= 5:
        return False
    if (day.month, day.day) in {(1, 1), (5, 1), (12, 25), (12, 26)}:
        return False
    easter_sunday = easter(day.year)
    return day not in {easter_sunday - timedelta(days=2), easter_sunday + timedelta(days=1)}


def due_dates_charged_on(charge_date: date) -> list[date]:
    """
    The due dates a debit charged on ``charge_date`` can be for: that day,
    and the non-business days right before it, which roll forward to it.
    """
    dates = [charge_date]
    day = charge_date - timedelta(days=1)
    while not is_bank_business_day(day):
        dates.append(day)
        day -= timedelta(days=1)
    return dates


def charge_date_for(due_date: date) -> date:
    """The day a debit due on ``due_date`` is charged: the first business day from it."""
    day = due_date
    while not is_bank_business_day(day):
        day += timedelta(days=1)
    return day


def find_charged_invoice(transaction):
    """
    The one purchase invoice that ``transaction`` pays, or None when there is
    none or more than one could be it.
    """
    if transaction.amount.amount >= 0:
        return None
    Invoice = apps.get_model("invoices", "Invoice")
    # Credit notes are paid to us, never by us
    qs = Invoice.objects.filter(
        company=transaction.company,
        payment_status__in=["unpaid", "partial"],
        due_date__in=due_dates_charged_on(transaction.transaction_date),
    ).exclude(document_type="credit_note")
    matches = list(
        annotate_amount_remaining(qs, Invoice).filter(db_amount_remaining=abs(transaction.amount.amount))[:2]
    )
    return matches[0] if len(matches) == 1 else None


def find_waiting_charge(invoice):
    """
    The one unreconciled charge already in the bank that pays ``invoice``, or
    None. That charge has to pick ``invoice`` back (find_charged_invoice),
    so both sides agree no other candidate exists.
    """
    if not invoice.due_date or not invoice.company_id or invoice.document_type == "credit_note":
        return None
    remaining = invoice.amount_remaining_decimal
    if remaining <= 0:
        return None
    BankTransaction = apps.get_model("banking", "BankTransaction")
    charges = list(
        BankTransaction.objects.filter(
            company_id=invoice.company_id,
            reconciliation_status="unreconciled",
            transaction_date=charge_date_for(invoice.due_date),
            amount=-remaining,
        )[:2]
    )
    if len(charges) != 1 or find_charged_invoice(charges[0]) != invoice:
        return None
    return charges[0]
