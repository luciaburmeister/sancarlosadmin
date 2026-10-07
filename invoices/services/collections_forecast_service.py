from datetime import timedelta
from decimal import Decimal
from itertools import groupby

from django.contrib.postgres.aggregates import BoolOr
from django.core.cache import cache
from django.db.models import (
    BooleanField,
    Case,
    Count,
    DateField,
    DecimalField,
    ExpressionWrapper,
    F,
    Min,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from invoices.models import (
    CollectionsForecastEmailSettings,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from payments.mixins import annotate_amount_remaining, annotate_forecast_date
from sales.models import Client


class CollectionsForecastService:
    """
    Builds the collections forecast for one company -- the single source
    of truth for these numbers: both the calendar view and the
    scheduled/manual email report call build_forecast() so they can never
    drift apart.
    """

    # Same TTL used by the other dashboards (operational / accounts payable).
    CACHE_TTL = 300

    ZERO = Value(Decimal("0.00"))
    DECIMAL_FIELD = DecimalField(max_digits=12, decimal_places=2)
    IS_PROFORMA = Case(
        When(status=SalesInvoiceStatus.PROFORMA, then=True),
        default=False,
        output_field=BooleanField(),
    )

    @classmethod
    def _is_late_commitment_expr(cls, threshold_days):
        """
        An invoice is "late" when its payment commitment date was agreed
        more than threshold_days after its due date -- both dates must be
        set for the comparison to make sense. threshold_days is per-company
        (CollectionsForecastEmailSettings.late_commitment_threshold_days).
        """
        return Case(
            When(
                Q(payment_commitment_date__isnull=False)
                & Q(due_date__isnull=False)
                & Q(
                    payment_commitment_date__gt=ExpressionWrapper(
                        F("due_date") + timedelta(days=threshold_days),
                        output_field=DateField(),
                    )
                ),
                then=True,
            ),
            default=False,
            output_field=BooleanField(),
        )

    @staticmethod
    def _cache_key(company, date_from, date_to) -> str:
        return f"collections_forecast:{company.pk}:{date_from.isoformat()}:{date_to.isoformat()}"

    @classmethod
    def invalidate_cache(cls, company, date_from, date_to) -> None:
        cache.delete(cls._cache_key(company, date_from, date_to))

    @staticmethod
    def _pending_queryset(company):
        """
        The not-fully-paid, FINAL/PRO FORMA SalesInvoice rows a collections
        forecast is built from, annotated with db_amount_remaining and
        forecast_date -- single source of truth for "what counts", reused
        by build_forecast() and by anything else (e.g. the commitment-date
        editor) that needs to resolve exactly which invoices make up a
        client's row in a given bucket.
        """
        pending_qs = SalesInvoice.objects.filter(
            company=company,
            status__in=[SalesInvoiceStatus.FINAL, SalesInvoiceStatus.PROFORMA],
        ).exclude(payment_status=InvoicePaymentStatus.PAID)
        pending_qs = annotate_amount_remaining(pending_qs, SalesInvoice)
        return annotate_forecast_date(pending_qs)

    @classmethod
    def row_invoices(cls, company, client_id, forecast_date=None, overdue_before=None):
        """
        The exact SalesInvoice rows behind one client's row in one bucket --
        same conditions _build_bucket used to compute that row's totals and
        Client.invoices_admin_url used to build its "View Invoices" link.
        Pass exactly one of forecast_date (a single-day bucket) or
        overdue_before (the overdue bucket).
        """
        qs = cls._pending_queryset(company).filter(client_id=client_id)
        if forecast_date is not None:
            return qs.filter(forecast_date=forecast_date)
        return qs.filter(forecast_date__lt=overdue_before)

    @classmethod
    def build_forecast(cls, company, date_from, date_to) -> dict:
        """
        Builds the collections forecast for one company over the calendar
        range [date_from, date_to] (both inclusive, both chosen by the user
        on the dashboard):

        - "overdue": one bucket for everything due before date_from, grouped
          by client -- when date_from is today (the default), this is
          exactly "everything already overdue".
        - "buckets": ONE ENTRY PER CALENDAR DAY in [date_from, date_to] --
          including days with no invoices due (they show as an empty bucket) --
          so the view reads as an actual calendar, not just a list of the days
          that happen to have data.
        - "window_total"/"window_count": totals across the whole visible range
          (sum of all buckets).
        - "month_total"/"month_count": everything due in the current real
          calendar month, regardless of the selected range.

        This is the single source of truth for the numbers: both the calendar
        view and the scheduled/manual email report call this function,
        so they can never drift apart. Cached per company/date range for 5
        minutes, same convention as the operational and accounts-payable
        dashboards.

        Every FINAL and PRO FORMA, unpaid invoice counts -- cancelled and
        already-paid invoices are irrelevant to a collections forecast. Each
        invoice is placed on its "forecast date": the payment commitment
        date if the client agreed to one, otherwise the due date, otherwise
        the issue date -- so an invoice is never dropped from the forecast
        just because one of those dates is missing.
        """
        if not company:
            return cls._empty_result(date_from, date_to)

        today = timezone.localdate()
        cache_key = cls._cache_key(company, date_from, date_to)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        late_threshold_days = (
            CollectionsForecastEmailSettings.objects.filter(company=company)
            .values_list("late_commitment_threshold_days", flat=True)
            .first()
        )
        if late_threshold_days is None:
            late_threshold_days = CollectionsForecastEmailSettings._meta.get_field(
                "late_commitment_threshold_days"
            ).default
        is_late_commitment_expr = cls._is_late_commitment_expr(late_threshold_days)

        pending_qs = cls._pending_queryset(company)

        # ── Overdue bucket ────────────────────────────────────────────────
        overdue_rows = list(
            pending_qs.filter(forecast_date__lt=date_from)
            .values("client_id", "client__name")
            .annotate(
                day_total=Coalesce(Sum("db_amount_remaining"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                total_owed=Coalesce(Sum("total_amount"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                day_count=Count("id"),
                sample_invoice_id=Min("id"),
                has_proforma=BoolOr(cls.IS_PROFORMA),
                has_late_commitment=BoolOr(is_late_commitment_expr),
            )
            .order_by("-day_total", "client__name")
        )
        overdue = cls._build_bucket(overdue_rows, overdue_before=date_from)

        # ── Date buckets: one row per calendar day, even with no invoices ──
        window_rows = list(
            pending_qs.filter(forecast_date__gte=date_from, forecast_date__lte=date_to)
            .values("forecast_date", "client_id", "client__name")
            .annotate(
                day_total=Coalesce(Sum("db_amount_remaining"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                total_owed=Coalesce(Sum("total_amount"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                day_count=Count("id"),
                sample_invoice_id=Min("id"),
                has_proforma=BoolOr(cls.IS_PROFORMA),
                has_late_commitment=BoolOr(is_late_commitment_expr),
            )
            .order_by("forecast_date", "-day_total", "client__name")
        )
        rows_by_date = {
            forecast_date: list(rows)
            for forecast_date, rows in groupby(window_rows, key=lambda r: r["forecast_date"])
        }

        days_in_range = (date_to - date_from).days
        buckets = []
        for offset in range(days_in_range + 1):
            bucket_date = date_from + timedelta(days=offset)
            buckets.append({
                "date": bucket_date,
                **cls._build_bucket(
                    rows_by_date.get(bucket_date, []), forecast_date=bucket_date,
                ),
            })

        window_total = sum((b["total"] for b in buckets), Decimal("0.00"))
        window_count = sum(b["count"] for b in buckets)

        # ── Month total: everything due this calendar month ────────────────
        month_start = today.replace(day=1)
        next_month_start = (month_start + timedelta(days=32)).replace(day=1)
        month_agg = pending_qs.filter(
            forecast_date__gte=month_start, forecast_date__lt=next_month_start
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
    def _build_bucket(rows, forecast_date=None, overdue_before=None) -> dict:
        """Turns a list of already client-aggregated rows into one bucket:
        its own total/count, plus the per-client breakdown. view_url is
        computed once here (via Client.invoices_admin_url) so the calendar,
        its email report, and their templates all share the same link --
        no separate URL-building logic anywhere else. Passes the exact same
        forecast_date condition used to build this bucket's rows, so the
        link can never disagree with the total it's attached to."""
        clients = [
            {
                "client_id": row["client_id"],
                "client_name": row["client__name"],
                "day_total": row["day_total"],
                "day_count": row["day_count"],
                "total_owed": row["total_owed"],
                "sample_invoice_id": row["sample_invoice_id"],
                "view_url": Client(pk=row["client_id"]).invoices_admin_url(
                    forecast_date=forecast_date, overdue_before=overdue_before
                ),
                # True when at least one invoice in this row is still a pro
                # forma -- shown as a small badge in the calendar so the user
                # knows this amount isn't fully invoiced yet.
                "has_proforma": row["has_proforma"],
                # True when at least one invoice in this row has a payment
                # commitment date more than 30 days after its due date.
                "has_late_commitment": row["has_late_commitment"],
            }
            for row in rows
        ]
        return {
            "total": sum((c["day_total"] for c in clients), Decimal("0.00")),
            "count": sum(c["day_count"] for c in clients),
            "clients": clients,
        }

    @staticmethod
    def _empty_bucket() -> dict:
        return {"total": Decimal("0.00"), "count": 0, "clients": []}

    @classmethod
    def _empty_result(cls, date_from, date_to) -> dict:
        return {
            "overdue": cls._empty_bucket(),
            "buckets": [],
            "window_total": Decimal("0.00"),
            "window_count": 0,
            "month_total": Decimal("0.00"),
            "month_count": 0,
            "today": timezone.localdate(),
            "date_from": date_from,
            "date_to": date_to,
        }
