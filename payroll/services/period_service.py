"""
Period close workflow for payroll.

Closing a period freezes it and aggregates the per-employee figures into
period-level totals plus the expected payment dates, which is what the
cashflow projection (Etapa 4) consumes. A period with unresolved pending
import rows cannot be closed.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from payroll.models import PayrollPeriod

ZERO = Decimal("0.00")
DEFAULT_SS_OFFSET_DAYS = 30


def _last_day_of_month(year: int, month: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last)


def _last_business_day(year: int, month: int) -> date:
    day = _last_day_of_month(year, month)
    # Saturday = 5, Sunday = 6 -> walk back to Friday
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def compute_period_totals(period: PayrollPeriod) -> dict:
    """Aggregate the period's employee payrolls into period-level totals."""
    agg = period.payrolls.aggregate(
        net=Sum("net_salary"),
        employer_ss=Sum("employer_social_security"),
        employee_ss=Sum("employee_social_security"),
        irpf=Sum("irpf_withholding_amount"),
        cost=Sum("employer_cost"),
    )
    return {
        "total_net_salary": agg["net"] or ZERO,
        "total_employer_ss": agg["employer_ss"] or ZERO,
        "total_employee_ss": agg["employee_ss"] or ZERO,
        "total_irpf_withheld": agg["irpf"] or ZERO,
        "total_employer_cost": agg["cost"] or ZERO,
    }


def compute_payment_dates(period: PayrollPeriod) -> dict:
    """Derive expected salary / Social Security payment dates from the
    company's payroll configuration (sensible defaults when missing)."""
    config = getattr(period.company, "payroll_config", None)
    salary_day = getattr(config, "salary_payment_day", 0) or 0
    ss_offset = (
        getattr(config, "ss_payment_offset_days", DEFAULT_SS_OFFSET_DAYS)
        if config is not None
        else DEFAULT_SS_OFFSET_DAYS
    )

    if salary_day and 1 <= salary_day <= 31:
        last = calendar.monthrange(period.year, period.month)[1]
        salary_date = date(period.year, period.month, min(salary_day, last))
    else:
        salary_date = _last_business_day(period.year, period.month)

    ss_date = _last_day_of_month(period.year, period.month) + timedelta(days=ss_offset)

    return {"salary_payment_date": salary_date, "ss_payment_date": ss_date}


@transaction.atomic
def close_period(period: PayrollPeriod) -> PayrollPeriod:
    """Aggregate totals, set payment dates and mark the period as closed.

    Raises ``ValidationError`` if the period is already closed or still has
    unresolved pending import rows.
    """
    if period.status == PayrollPeriod.Status.CLOSED:
        raise ValidationError(_("This period is already closed."))

    pending = period.pending_rows.count()
    if pending:
        raise ValidationError(
            _(
                "Cannot close: %(n)d employee(s) from the import are still "
                "unmatched. Resolve every pending row first."
            )
            % {"n": pending}
        )

    if not period.payrolls.exists():
        raise ValidationError(
            _("Cannot close an empty period (no employee payrolls).")
        )

    for field, value in compute_period_totals(period).items():
        setattr(period, field, value)
    for field, value in compute_payment_dates(period).items():
        setattr(period, field, value)

    period.status = PayrollPeriod.Status.CLOSED
    period.closed_at = timezone.now()
    period.save()
    return period


@transaction.atomic
def reopen_period(period: PayrollPeriod) -> PayrollPeriod:
    """Reopen a closed period so it can be re-imported / edited."""
    if period.status != PayrollPeriod.Status.CLOSED:
        raise ValidationError(_("Only closed periods can be reopened."))

    period.status = PayrollPeriod.Status.DRAFT
    period.closed_at = None
    period.save(update_fields=["status", "closed_at", "updated_at"])
    return period
