# finance/services/payment_calendar.py
from __future__ import annotations

import dataclasses
import datetime
from decimal import Decimal

from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from company.models import BankAccount, Company
from invoices.models import Invoice, InvoicePaymentStatus, SalesInvoice, SalesInvoiceStatus
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
    "Social security",
    "IRPF",
    "IVA",
    "IS",
    "Financing",
]

INCOME_CATEGORIES = [
    "Invoice collections",
    "Projected sale",
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
        .filter(company=company, due_date__range=(date_from, date_to))
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .select_related("provider")
    )
    return [
        CalendarEntry(
            date=invoice.due_date,
            amount=invoice.amount_remaining.amount,
            direction=CalendarDirection.EXPENSE,
            category="Invoice payments",
            description=f"{invoice.invoice_number or 'Factura'} — {invoice.provider}",
            source_model="invoices.Invoice",
            source_id=invoice.pk,
            admin_url=_admin_change_url("invoices.Invoice", invoice.pk),
        )
        for invoice in invoices
    ]


def _sales_invoice_entries(company: Company, date_from: datetime.date, date_to: datetime.date) -> list[CalendarEntry]:
    invoices = (
        SalesInvoice.objects
        .filter(company=company, status=SalesInvoiceStatus.FINAL, due_date__range=(date_from, date_to))
        .exclude(payment_status=InvoicePaymentStatus.PAID)
        .select_related("client")
    )
    return [
        CalendarEntry(
            date=invoice.due_date,
            amount=invoice.amount_remaining.amount,
            direction=CalendarDirection.INCOME,
            category="Invoice collections",
            description=f"{invoice.invoice_number or 'Factura'} — {invoice.client}",
            source_model="invoices.SalesInvoice",
            source_id=invoice.pk,
            admin_url=_admin_change_url("invoices.SalesInvoice", invoice.pk),
        )
        for invoice in invoices
    ]


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
            category=period.rule.tax_type.tax_name,
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
            category="Social security",
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


def build_payment_calendar(company: Company, bank_accounts, date_from: datetime.date, date_to: datetime.date) -> dict[datetime.date, dict]:
    entries = (
        _loan_installment_entries(company, date_from, date_to)
        + _purchase_invoice_entries(company, date_from, date_to)
        + _sales_invoice_entries(company, date_from, date_to)
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

    running_balance = _get_opening_balance(bank_accounts)
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

    by_category = {
        category: {day: Decimal("0.00") for day in days}
        for category in EXPENSE_CATEGORIES + INCOME_CATEGORIES
    }
    entries_by_category_and_day = {
        category: {day: [] for day in days}
        for category in EXPENSE_CATEGORIES + INCOME_CATEGORIES
    }

    for day in days:
        for entry in calendar_days[day]["items"]:
            if entry.category in by_category:
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
        "salida_rows": _rows(EXPENSE_CATEGORIES),
        "entrada_rows": _rows(INCOME_CATEGORIES),
        "entradas_cells": _cells({day: calendar_days[day]["entradas"] for day in days}),
        "salidas_cells": _cells({day: calendar_days[day]["salidas"] for day in days}),
        "saldo_cells": _cells({day: calendar_days[day]["saldo"] for day in days}),
    }