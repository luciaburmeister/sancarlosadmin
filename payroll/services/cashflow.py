"""
Payroll cash-flow projection.

Turns *closed* payroll periods into weekly cash outflow buckets that the
Accounts Payable dashboard can show alongside supplier invoices:

* salaries leave the bank on ``salary_payment_date`` (``total_net_salary``)
* the TGSS settlement leaves on ``ss_payment_date``
  (``total_employer_ss`` + ``total_employee_ss`` withheld)

IRPF is paid quarterly to the AEAT and is reported as an informational total
only (not bucketed here). Only closed periods are considered, since draft
periods have no computed totals or payment dates yet.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from payroll.models import PayrollPeriod

ZERO = Decimal("0.00")
_FIELD = DecimalField(max_digits=14, decimal_places=2)


def build_week_ranges(date_from: date, date_to: date) -> list[dict]:
    """Weekly buckets (7-day windows) covering ``[date_from, date_to]``."""
    weeks = []
    cursor = date_from
    while cursor <= date_to:
        week_end = min(cursor + timedelta(days=6), date_to)
        weeks.append(
            {"start": cursor, "end": week_end, "label": cursor.strftime("%d %b")}
        )
        cursor = week_end + timedelta(days=1)
    return weeks


def get_payroll_outflows(company_ids, date_from, date_to, weeks=None) -> dict:
    """Aggregate closed-period payroll outflows into weekly buckets.

    ``weeks`` may be supplied to align exactly with another projection
    (e.g. the Accounts Payable cash-flow); otherwise it is derived from the
    date range. Returns floats, ready for JSON serialization.
    """
    if weeks is None:
        weeks = build_week_ranges(date_from, date_to)

    company_ids = list(company_ids)
    if not company_ids:
        return _empty_result(weeks)

    periods = PayrollPeriod.objects.filter(
        status=PayrollPeriod.Status.CLOSED,
        company_id__in=company_ids,
    )

    agg_kwargs = {}
    for idx, w in enumerate(weeks):
        agg_kwargs[f"salary_{idx}"] = Coalesce(
            Sum(
                "total_net_salary",
                filter=Q(
                    salary_payment_date__gte=w["start"],
                    salary_payment_date__lte=w["end"],
                ),
            ),
            Value(ZERO),
            output_field=_FIELD,
        )
        agg_kwargs[f"ss_{idx}"] = Coalesce(
            Sum(
                F("total_employer_ss") + F("total_employee_ss"),
                filter=Q(
                    ss_payment_date__gte=w["start"],
                    ss_payment_date__lte=w["end"],
                ),
            ),
            Value(ZERO),
            output_field=_FIELD,
        )

    # Range-wide totals (for KPIs).
    agg_kwargs["net_total"] = Coalesce(
        Sum(
            "total_net_salary",
            filter=Q(
                salary_payment_date__gte=date_from,
                salary_payment_date__lte=date_to,
            ),
        ),
        Value(ZERO),
        output_field=_FIELD,
    )
    agg_kwargs["ss_total"] = Coalesce(
        Sum(
            F("total_employer_ss") + F("total_employee_ss"),
            filter=Q(
                ss_payment_date__gte=date_from,
                ss_payment_date__lte=date_to,
            ),
        ),
        Value(ZERO),
        output_field=_FIELD,
    )
    agg_kwargs["irpf_total"] = Coalesce(
        Sum(
            "total_irpf_withheld",
            filter=Q(
                salary_payment_date__gte=date_from,
                salary_payment_date__lte=date_to,
            ),
        ),
        Value(ZERO),
        output_field=_FIELD,
    )

    agg = periods.aggregate(**agg_kwargs)

    week_rows = []
    for idx, w in enumerate(weeks):
        salary = float(agg[f"salary_{idx}"])
        ss = float(agg[f"ss_{idx}"])
        week_rows.append(
            {
                "label": w["label"],
                "salary": salary,
                "ss": ss,
                "amount": round(salary + ss, 2),
            }
        )

    net_total = float(agg["net_total"])
    ss_total = float(agg["ss_total"])
    return {
        "weeks": week_rows,
        "net_total": net_total,
        "ss_total": ss_total,
        "irpf_total": float(agg["irpf_total"]),
        "total": round(net_total + ss_total, 2),
    }


def _empty_result(weeks) -> dict:
    return {
        "weeks": [
            {"label": w["label"], "salary": 0.0, "ss": 0.0, "amount": 0.0}
            for w in weeks
        ],
        "net_total": 0.0,
        "ss_total": 0.0,
        "irpf_total": 0.0,
        "total": 0.0,
    }
