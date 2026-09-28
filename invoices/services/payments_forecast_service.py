from datetime import timedelta
from decimal import Decimal
from itertools import groupby

from django.contrib.postgres.aggregates import BoolOr
from django.core.cache import cache
from django.db.models import BooleanField, Case, Count, DecimalField, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from invoices.models import Invoice, InvoicePaymentStatus
from materials.models import PaymentMethod, Provider
from payments.mixins import annotate_amount_remaining, annotate_effective_date


class PaymentsForecastService:
    """
    Builds the payments forecast for one company -- the single source of
    truth for these numbers: both the calendar view and the
    scheduled/manual email report call build_forecast() so they can never
    drift apart.
    """

    CACHE_TTL = 300

    ZERO = Value(Decimal("0.00"))
    DECIMAL_FIELD = DecimalField(max_digits=12, decimal_places=2)
    IS_SEPA = Case(
        When(payment_method=PaymentMethod.SEPA_DIRECT_DEBIT, then=True),
        default=False,
        output_field=BooleanField(),
    )

    @classmethod
    def build_forecast(cls, company, date_from, date_to, payment_methods: list | None = None) -> dict:
        """
        Builds the payments forecast for one company over the calendar range
        [date_from, date_to] (both inclusive, both chosen by the user on the
        dashboard):

        - "overdue": one bucket for everything due before date_from, grouped
          by provider -- when date_from is today (the default), this is
          exactly "everything already overdue".
        - "buckets": ONE ENTRY PER CALENDAR DAY in [date_from, date_to] --
          including days with no invoices due (they show as an empty bucket) --
          same convention as the Collections Forecast calendar.
        - "window_total"/"window_count": totals across the whole visible range.
        - "month_total"/"month_count": everything due in the current real
          calendar month, regardless of the selected range.

        Amounts use the real outstanding balance per invoice (total_amount minus
        whatever has already been applied via PaymentAllocation), via the same
        annotate_amount_remaining() helper the Accounts Payable dashboard uses --
        so a partially paid invoice never overstates what's actually owed. This
        is the single source of truth for the numbers: both the calendar view
        and the scheduled/manual email report call this function.

        Invoices of any confirmation status (approved, pending review, or
        declined) count -- only already-paid invoices are excluded. Bucketed
        by their effective_date (payments.mixins.annotate_effective_date:
        commitment_date, else due_date, else date_issued), so an invoice
        with no due_date still shows up instead of silently dropping out of
        the calendar. Optionally restricted to a subset of payment methods
        (SEPA, transfer, etc.).
        """
        if not company:
            return cls._empty_result(date_from, date_to)

        today = timezone.now().date()
        methods_key = ",".join(sorted(payment_methods)) if payment_methods else "all"
        cache_key = f"payments_forecast:{company.pk}:{date_from.isoformat()}:{date_to.isoformat()}:{methods_key}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        pending_qs = Invoice.objects.filter(company=company).exclude(
            payment_status=InvoicePaymentStatus.PAID
        )

        if payment_methods:
            pending_qs = pending_qs.filter(payment_method__in=payment_methods)

        pending_qs = annotate_amount_remaining(pending_qs, Invoice)
        pending_qs = annotate_effective_date(pending_qs).filter(effective_date__isnull=False)

        overdue_rows = list(
            pending_qs.filter(effective_date__lt=date_from)
            .values("provider_id", "provider__name")
            .annotate(
                day_total=Coalesce(Sum("db_amount_remaining"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                total_owed=Coalesce(Sum("total_amount"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                day_count=Count("id"),
                has_sepa=BoolOr(cls.IS_SEPA),
            )
            .order_by("provider__name")
        )
        overdue = cls._build_bucket(overdue_rows, overdue_before=date_from)

        window_rows = list(
            pending_qs.filter(effective_date__gte=date_from, effective_date__lte=date_to)
            .values("effective_date", "provider_id", "provider__name")
            .annotate(
                day_total=Coalesce(Sum("db_amount_remaining"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                total_owed=Coalesce(Sum("total_amount"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                day_count=Count("id"),
                has_sepa=BoolOr(cls.IS_SEPA),
            )
            .order_by("effective_date", "provider__name")
        )
        rows_by_date = {
            effective_date: list(rows)
            for effective_date, rows in groupby(window_rows, key=lambda r: r["effective_date"])
        }

        days_in_range = (date_to - date_from).days
        buckets = []
        for offset in range(days_in_range + 1):
            bucket_date = date_from + timedelta(days=offset)
            buckets.append({
                "date": bucket_date,
                **cls._build_bucket(
                    rows_by_date.get(bucket_date, []), effective_date=bucket_date,
                ),
            })

        window_total = sum((b["total"] for b in buckets), Decimal("0.00"))
        window_count = sum(b["count"] for b in buckets)

        # ── Month total: everything due this calendar month ────────────────
        month_start = today.replace(day=1)
        next_month_start = (month_start + timedelta(days=32)).replace(day=1)
        month_agg = pending_qs.filter(
            effective_date__gte=month_start, effective_date__lt=next_month_start
        ).aggregate(
            total=Coalesce(Sum("db_amount_remaining"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
            count=Count("id"),
        )

        result = {
            "overdue": overdue,
            "buckets": buckets,
            "window_total": window_total,
            "window_count": window_count,
            "month_total": month_agg["total"],
            "month_count": month_agg["count"],
            "today": today,
            "date_from": date_from,
            "date_to": date_to,
        }
        cache.set(cache_key, result, cls.CACHE_TTL)
        return result

    @staticmethod
    def _build_bucket(rows, effective_date=None, overdue_before=None) -> dict:
        """Turns a list of already provider-aggregated rows into one bucket:
        its own total/count, plus the per-provider breakdown. view_url is
        computed once here (via Provider.invoices_admin_url) so the calendar,
        its email report, and their templates all share the same link --
        no separate URL-building logic anywhere else. Passes the exact same
        effective_date condition used to build this bucket's rows, so the
        link can never disagree with the total it's attached to."""
        providers = [
            {
                "provider_id": row["provider_id"],
                "provider_name": row["provider__name"],
                "day_total": row["day_total"],
                "day_count": row["day_count"],
                "total_owed": row["total_owed"],
                "view_url": Provider(pk=row["provider_id"]).invoices_admin_url(
                    effective_date=effective_date, overdue_before=overdue_before
                ),
                # True when at least one invoice in this row is paid by SEPA
                # direct debit -- shown as a small badge in the calendar so a
                # SEPA due date (which triggers a bank charge, unlike a
                # transfer the provider has to chase) stands out.
                "has_sepa": row["has_sepa"],
            }
            for row in rows
        ]
        return {
            "total": sum((p["day_total"] for p in providers), Decimal("0.00")),
            "count": sum(p["day_count"] for p in providers),
            "providers": providers,
        }

    @staticmethod
    def _empty_bucket() -> dict:
        return {"total": Decimal("0.00"), "count": 0, "providers": []}

    @classmethod
    def _empty_result(cls, date_from, date_to) -> dict:
        return {
            "overdue": cls._empty_bucket(),
            "buckets": [],
            "window_total": Decimal("0.00"),
            "window_count": 0,
            "month_total": Decimal("0.00"),
            "month_count": 0,
            "today": timezone.now().date(),
            "date_from": date_from,
            "date_to": date_to,
        }
