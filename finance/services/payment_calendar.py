# finance/services/payment_calendar.py
from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from django.db.models.functions import Coalesce
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from banking.models import BankTransaction
from company.models import BankAccount, Company
from invoices.models import ConfirmationStatus, Invoice, InvoicePaymentStatus, SalesInvoice, SalesInvoiceStatus
from loans.models import LoanInstallment, LoanInstallmentPaymentStatus
from finance.models import TaxPeriod, TaxPeriodStatus
from payroll.models import PayrollPeriod


class CalendarDirection:
    INCOME = "income"
    EXPENSE = "expense"


EXPENSE_CATEGORIES = [
    "Invoice payments",
    "Projected supplier payment plan",
    "Projected variable cost",
    "Fixed costs",
    "Payroll",
    "Taxes",
    "Financing",
]

INCOME_CATEGORIES = [
    "Invoice collections",
    "Proforma",
    "Work pending invoicing",
]


def _admin_change_url(source_model: str, source_id: int) -> str:
    app_label, model_name = source_model.split(".")
    try:
        return reverse(f"admin:{app_label}_{model_name.lower()}_change", args=[source_id])
    except NoReverseMatch:
        return ""


@dataclasses.dataclass(frozen=True)
class CalendarEntry:
    date: datetime.date
    amount: Decimal
    direction: str
    category: str
    description: str
    source_model: str
    source_id: int
    admin_url: str


def _loan_installment_entries(company: Company, date_from: datetime.date, date_to: datetime.date) -> list[CalendarEntry]:
    installments = (
        LoanInstallment.objects
        .filter(loan__company=company, due_date__range=(date_from, date_to))
        .exclude(payment_status=LoanInstallmentPaymentStatus.PAID)
        .select_related("loan", "loan__bank")
    )
    return [
        CalendarEntry(
            date=installment.due_date,
            amount=installment.amount_remaining.amount,
            direction=CalendarDirection.EXPENSE,
            category="Financing",
            description=f"{installment.loan} — cuota #{installment.installment_number}",
            source_model="loans.Loan",
            source_id=installment.loan_id,
            admin_url=_admin_change_url("loans.Loan", installment.loan_id),
        )
        for installment in installments
    ]


def _purchase_invoice_entries(company: Company, date_from: datetime.date, date_to: datetime.date) -> list[CalendarEntry]:
    invoices = (
        Invoice.objects
        .annotate(effective_date=Coalesce("commitment_date", "due_date"))
        .filter(company=company, effective_date__range=(date_from, date_to), confirmation_status=ConfirmationStatus.APPROVED)
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .select_related("provider")
        .order_by("provider__name", "effective_date")
    )

    grouped: dict[tuple[datetime.date, int], dict] = {}
    for invoice in invoices:
        key = (invoice.effective_date, invoice.provider_id)
        group = grouped.setdefault(key, {"provider": invoice.provider, "amount": Decimal("0.00"), "invoice_ids": []})
        group["amount"] += invoice.amount_remaining.amount
        group["invoice_ids"].append(invoice.pk)

    entries = []
    for (effective_date, _provider_id), group in grouped.items():
        is_single = len(group["invoice_ids"]) == 1
        entries.append(
            CalendarEntry(
                date=effective_date,
                amount=group["amount"],
                direction=CalendarDirection.EXPENSE,
                category="Invoice payments",
                description=str(group["provider"]),
                source_model="invoices.Invoice",
                source_id=group["invoice_ids"][0],
                admin_url=_admin_change_url("invoices.Invoice", group["invoice_ids"][0]) if is_single else "",
            )
        )
    return entries


def _sales_invoice_entries(company: Company, date_from: datetime.date, date_to: datetime.date) -> list[CalendarEntry]:
    invoices = (
        SalesInvoice.objects
        .filter(company=company, status=SalesInvoiceStatus.FINAL, due_date__range=(date_from, date_to))
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .select_related("client")
        .order_by("client__name", "due_date")
    )

    grouped: dict[tuple[datetime.date, int], dict] = {}
    for invoice in invoices:
        key = (invoice.due_date, invoice.client_id)
        group = grouped.setdefault(key, {"client": invoice.client, "amount": Decimal("0.00"), "invoice_ids": []})
        group["amount"] += invoice.amount_remaining.amount
        group["invoice_ids"].append(invoice.pk)

    entries = []
    for (due_date, _client_id), group in grouped.items():
        is_single = len(group["invoice_ids"]) == 1
        entries.append(
            CalendarEntry(
                date=due_date,
                amount=group["amount"],
                direction=CalendarDirection.INCOME,
                category="Invoice collections",
                description=str(group["client"]),
                source_model="invoices.SalesInvoice",
                source_id=group["invoice_ids"][0],
                admin_url=_admin_change_url("invoices.SalesInvoice", group["invoice_ids"][0]) if is_single else "",
            )
        )
    return entries


def _proforma_entries(company: Company, date_from: datetime.date, date_to: datetime.date) -> list[CalendarEntry]:
    invoices = (
        SalesInvoice.objects
        .filter(company=company, status=SalesInvoiceStatus.PROFORMA, due_date__range=(date_from, date_to))
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .select_related("client")
        .order_by("client__name", "due_date")
    )

    grouped: dict[tuple[datetime.date, int], dict] = {}
    for invoice in invoices:
        key = (invoice.due_date, invoice.client_id)
        group = grouped.setdefault(key, {"client": invoice.client, "amount": Decimal("0.00"), "invoice_ids": []})
        group["amount"] += invoice.amount_remaining.amount
        group["invoice_ids"].append(invoice.pk)

    entries = []
    for (due_date, _client_id), group in grouped.items():
        is_single = len(group["invoice_ids"]) == 1
        entries.append(
            CalendarEntry(
                date=due_date,
                amount=group["amount"],
                direction=CalendarDirection.INCOME,
                category="Proforma",
                description=str(group["client"]),
                source_model="invoices.SalesInvoice",
                source_id=group["invoice_ids"][0],
                admin_url=_admin_change_url("invoices.SalesInvoice", group["invoice_ids"][0]) if is_single else "",
            )
        )
    return entries


def _tax_period_entries(company: Company, date_from: datetime.date, date_to: datetime.date) -> list[CalendarEntry]:
    periods = (
        TaxPeriod.objects
        .filter(company=company, due_date__range=(date_from, date_to))
        .exclude(status=TaxPeriodStatus.PAID)
        .select_related("rule", "rule__tax_type")
    )
    return [
        CalendarEntry(
            date=period.due_date,
            amount=period.net_after_credit.amount,
            direction=CalendarDirection.EXPENSE,
            category="Taxes",
            description=f"{period.rule.tax_type} — {period.date_from} a {period.date_to}",
            source_model="finance.TaxPeriod",
            source_id=period.pk,
            admin_url=_admin_change_url("finance.TaxPeriod", period.pk),
        )
        for period in periods
        if period.net_after_credit.amount > 0
    ]


def _payroll_salary_entries(company: Company, date_from: datetime.date, date_to: datetime.date) -> list[CalendarEntry]:
    periods = PayrollPeriod.objects.filter(company=company, salary_payment_date__range=(date_from, date_to))
    return [
        CalendarEntry(
            date=period.salary_payment_date,
            amount=period.total_net_salary,
            direction=CalendarDirection.EXPENSE,
            category="Payroll",
            description=f"Nóminas {period.month:02d}/{period.year}",
            source_model="payroll.PayrollPeriod",
            source_id=period.pk,
            admin_url=_admin_change_url("payroll.PayrollPeriod", period.pk),
        )
        for period in periods
        if period.total_net_salary
    ]


def _payroll_ss_entries(company: Company, date_from: datetime.date, date_to: datetime.date) -> list[CalendarEntry]:
    periods = PayrollPeriod.objects.filter(company=company, ss_payment_date__range=(date_from, date_to))
    return [
        CalendarEntry(
            date=period.ss_payment_date,
            amount=period.total_employer_ss + period.total_employee_ss,
            direction=CalendarDirection.EXPENSE,
            category="Taxes",
            description=f"Seg. Social {period.month:02d}/{period.year}",
            source_model="payroll.PayrollPeriod",
            source_id=period.pk,
            admin_url=_admin_change_url("payroll.PayrollPeriod", period.pk),
        )
        for period in periods
        if period.total_employer_ss + period.total_employee_ss
    ]


def _get_opening_balance(bank_accounts) -> Decimal:
    accounts = bank_accounts.exclude(last_known_balance__isnull=True)
    return sum((account.last_known_balance.amount for account in accounts), Decimal("0.00"))


def _get_opening_balance_for_date(bank_accounts, date_from: datetime.date) -> Decimal:
    """
    Reconstructs the balance as it stood at date_from, anchored on
    last_known_balance (the balance as of today's last bank sync) and
    corrected using BankTransaction, the immutable ground truth of what
    actually moved in each account.

    - date_from == today: last_known_balance is already the answer.
    - date_from < today: last_known_balance already includes every real
      movement between date_from and today, so those are subtracted back
      out to recover the earlier balance.
    - date_from > today: last_known_balance predates any real movement
      that may already be booked between today and date_from, so those
      (if any — BankTransaction only holds settled movements, never
      projections) are added on top. Projected/expected cash flow for
      date_from itself is layered on separately by the calendar, not here.
    """
    base_balance = _get_opening_balance(bank_accounts)
    today = timezone.localdate()

    if date_from == today:
        return base_balance

    if date_from < today:
        moved_since = BankTransaction.objects.filter(
            bank_account__in=bank_accounts, transaction_date__gte=date_from, transaction_date__lte=today
        )
        moved_amount = sum((transaction.amount.amount for transaction in moved_since), Decimal("0.00"))
        return base_balance - moved_amount

    moved_until = BankTransaction.objects.filter(
        bank_account__in=bank_accounts, transaction_date__gt=today, transaction_date__lte=date_from
    )
    moved_amount = sum((transaction.amount.amount for transaction in moved_until), Decimal("0.00"))
    return base_balance + moved_amount


def build_payment_calendar(company: Company, bank_accounts, date_from: datetime.date, date_to: datetime.date) -> dict[datetime.date, dict]:
    entries = (
        _loan_installment_entries(company, date_from, date_to)
        + _purchase_invoice_entries(company, date_from, date_to)
        + _sales_invoice_entries(company, date_from, date_to)
        + _proforma_entries(company, date_from, date_to)
        + _tax_period_entries(company, date_from, date_to)
        + _payroll_salary_entries(company, date_from, date_to)
        + _payroll_ss_entries(company, date_from, date_to)
    )

    days: dict[datetime.date, dict] = {}
    current_date = date_from
    while current_date <= date_to:
        days[current_date] = {"entradas": Decimal("0.00"), "salidas": Decimal("0.00"), "items": []}
        current_date += datetime.timedelta(days=1)

    for entry in entries:
        day = days[entry.date]
        totals_key = "entradas" if entry.direction == CalendarDirection.INCOME else "salidas"
        day[totals_key] += entry.amount
        day["items"].append(entry)

    running_balance = _get_opening_balance_for_date(bank_accounts, date_from)
    for day_date in sorted(days.keys()):
        day = days[day_date]
        running_balance = running_balance + day["entradas"] - day["salidas"]
        day["saldo"] = running_balance

    return days


def build_calendar_matrix(company: Company, bank_accounts, date_from: datetime.date, date_to: datetime.date) -> dict:
    """
    Reshapes build_payment_calendar's per-day output into a fixed category × day
    matrix, matching the Excel layout. Every category row is always present —
    categories without a wired data source yet just show zero (rendered as "-"
    by the template) until we build that source.

    Each row carries, in addition to its totals per day (`cells`), a parallel
    `detail_cells` list — one entry per day, in the same order as `days` —
    containing that day's individual CalendarEntry objects, so the expanded
    detail can be rendered stacked directly under each day's column instead
    of as a single undifferentiated list.
    """
    calendar_days = build_payment_calendar(company, bank_accounts, date_from, date_to)
    days = sorted(calendar_days.keys())

    all_expense_categories = list(EXPENSE_CATEGORIES)
    all_income_categories = list(INCOME_CATEGORIES)
    for day in days:
        for entry in calendar_days[day]["items"]:
            target_list = all_expense_categories if entry.direction == CalendarDirection.EXPENSE else all_income_categories
            if entry.category not in target_list:
                target_list.append(entry.category)

    by_category = {category: {day: Decimal("0.00") for day in days} for category in all_expense_categories + all_income_categories}
    entries_by_category_and_day = {category: {day: [] for day in days} for category in all_expense_categories + all_income_categories}

    for day in days:
        for entry in calendar_days[day]["items"]:
            by_category[entry.category][day] += entry.amount
            entries_by_category_and_day[entry.category][day].append(entry)

    def _cells(values_by_day: dict) -> list[dict]:
        return [{"day": day, "value": values_by_day[day]} for day in days]

    def _rows(categories: list[str]) -> list[dict]:
        return [
            {
                "label": _(category),
                "cells": _cells(by_category[category]),
                "detail_cells": [entries_by_category_and_day[category][day] for day in days],
            }
            for category in categories
        ]

    return {
        "days": days,
        "salida_rows": _rows(all_expense_categories),
        "entrada_rows": _rows(all_income_categories),
        "entradas_cells": _cells({day: calendar_days[day]["entradas"] for day in days}),
        "salidas_cells": _cells({day: calendar_days[day]["salidas"] for day in days}),
        "saldo_cells": _cells({day: calendar_days[day]["saldo"] for day in days}),
    }