from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from djmoney.money import Money

from loans.models import AmortizationMethod, InterestDayCount, LoanInstallment, LoanInstallmentPaymentStatus

TWO_PLACES = Decimal("0.01")

FREQUENCY_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "annual": 12,
}


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _add_months(date, months):
    month = date.month - 1 + months
    year = date.year + month // 12
    month = month % 12 + 1
    day = min(date.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date.replace(year=year, month=month, day=day)


def _generate_due_dates(start_date, num_installments, months_per_period):
    due_dates = []
    d = start_date
    for _ in range(num_installments):
        d = _add_months(d, months_per_period)
        due_dates.append(d)
    return due_dates


def _schedule_params(loan):
    months_per_period = FREQUENCY_MONTHS[loan.payment_frequency]
    num_installments = loan.term_months // months_per_period
    annual_rate = Decimal(str(loan.interest_rate)) / Decimal("100")
    period_rate = annual_rate / (Decimal("12") / Decimal(months_per_period))
    return num_installments, period_rate, months_per_period, annual_rate


def generate_schedule(loan, force=False):
    if not force and loan.has_locked_schedule:
        raise ValueError("Cannot regenerate schedule: some installments already have payments.")

    num_installments, period_rate, months_per_period, annual_rate = _schedule_params(loan)
    currency = loan.principal_amount_currency
    principal = Decimal(str(loan.principal_amount.amount))
    due_dates = _generate_due_dates(loan.start_date, num_installments, months_per_period)
    use_act360 = loan.interest_day_count == InterestDayCount.ACT_360

    if loan.amortization_method == AmortizationMethod.FRENCH:
        if use_act360:
            rows = _french_act360(principal, annual_rate, due_dates, loan.start_date)
        else:
            rows = _french_30360(principal, period_rate, due_dates)
    elif loan.amortization_method == AmortizationMethod.GERMAN:
        if use_act360:
            rows = _german_act360(principal, annual_rate, due_dates, loan.start_date)
        else:
            rows = _german_30360(principal, period_rate, due_dates)
    else:
        # Bullet: day count convention doesn't affect the structure
        rows = _bullet(principal, period_rate, due_dates)

    with transaction.atomic():
        loan.installments.all().delete()
        objs = []
        for i, (principal_due, interest_due, outstanding_after, due_date) in enumerate(rows, start=1):
            objs.append(LoanInstallment(
                loan=loan,
                installment_number=i,
                due_date=due_date,
                principal_due=Money(principal_due, currency),
                interest_due=Money(interest_due, currency),
                total_due=Money(principal_due + interest_due, currency),
                amount_paid=Money(0, currency),
                payment_status=LoanInstallmentPaymentStatus.UNPAID,
                outstanding_principal_after=Money(outstanding_after, currency),
            ))
        LoanInstallment.objects.bulk_create(objs)


# ---------------------------------------------------------------------------
# 30/360 — equal monthly rate, perfectly constant installment
# ---------------------------------------------------------------------------

def _french_30360(principal, period_rate, due_dates):
    n = len(due_dates)
    if period_rate == 0:
        installment = principal / Decimal(n)
    else:
        installment = principal * period_rate / (1 - (1 + period_rate) ** -n)
    rows = []
    outstanding = principal
    for i, due_date in enumerate(due_dates, start=1):
        interest = outstanding * period_rate
        principal_due = installment - interest if i < n else outstanding
        outstanding = _round(outstanding - principal_due)
        rows.append((_round(principal_due), _round(interest), outstanding, due_date))
    return rows


def _german_30360(principal, period_rate, due_dates):
    n = len(due_dates)
    fixed_principal = principal / Decimal(n)
    rows = []
    outstanding = principal
    for i, due_date in enumerate(due_dates, start=1):
        interest = outstanding * period_rate
        principal_due = fixed_principal if i < n else outstanding
        outstanding = _round(outstanding - principal_due)
        rows.append((_round(principal_due), _round(interest), outstanding, due_date))
    return rows


def _bullet(principal, period_rate, due_dates):
    n = len(due_dates)
    rows = []
    for i, due_date in enumerate(due_dates, start=1):
        interest = principal * period_rate
        if i < n:
            rows.append((_round(Decimal("0.00")), _round(interest), _round(principal), due_date))
        else:
            rows.append((_round(principal), _round(interest), Decimal("0.00"), due_date))
    return rows


# ---------------------------------------------------------------------------
# ACT/360 — actual days between payment dates / 360 (Spanish bank standard)
# ---------------------------------------------------------------------------

def _act360_interest(outstanding, annual_rate, date_from, date_to):
    days = Decimal((date_to - date_from).days)
    return outstanding * annual_rate / Decimal("360") * days


def _french_act360(principal, annual_rate, due_dates, start_date):
    n = len(due_dates)
    period_rate = annual_rate / Decimal("12")
    if period_rate == 0:
        installment = principal / Decimal(n)
    else:
        installment = principal * period_rate / (1 - (1 + period_rate) ** -n)
    rows = []
    outstanding = principal
    prev_date = start_date
    for i, due_date in enumerate(due_dates, start=1):
        interest = _round(_act360_interest(outstanding, annual_rate, prev_date, due_date))
        principal_due = _round(installment - interest) if i < n else outstanding
        outstanding = _round(outstanding - principal_due)
        rows.append((principal_due, interest, outstanding, due_date))
        prev_date = due_date
    return rows


def _german_act360(principal, annual_rate, due_dates, start_date):
    n = len(due_dates)
    fixed_principal = _round(principal / Decimal(n))
    rows = []
    outstanding = principal
    prev_date = start_date
    for i, due_date in enumerate(due_dates, start=1):
        interest = _round(_act360_interest(outstanding, annual_rate, prev_date, due_date))
        principal_due = fixed_principal if i < n else outstanding
        outstanding = _round(outstanding - principal_due)
        rows.append((principal_due, interest, outstanding, due_date))
        prev_date = due_date
    return rows