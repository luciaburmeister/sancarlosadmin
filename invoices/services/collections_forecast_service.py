from datetime import timedelta
from decimal import Decimal
from itertools import groupby

from django.contrib.postgres.aggregates import BoolOr
from django.core.cache import cache
from django.db.models import BooleanField, Case, Count, DecimalField, Min, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from invoices.models import InvoicePaymentStatus, SalesInvoice, SalesInvoiceStatus
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

        FINAL and PRO FORMA, unpaid invoices with a due_date count --
        cancelled and already-paid invoices are irrelevant to a collections
        forecast.
        """
        if not company:
            return cls._empty_result(date_from, date_to)

        today = timezone.now().date()
        cache_key = f"collections_forecast:{company.pk}:{date_from.isoformat()}:{date_to.isoformat()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        pending_qs = (
            SalesInvoice.objects.filter(
                company=company,
                status__in=[SalesInvoiceStatus.FINAL, SalesInvoiceStatus.PROFORMA],
                due_date__isnull=False,
            )
            .exclude(payment_status=InvoicePaymentStatus.PAID)
        )

        # Total owed per client across ALL of their pending invoices (not just
        # the ones in the visible window) -- shown as context next to the
        # amount due on a specific day, so the user sees the full picture
        # before deciding whether to chase just this invoice or everything.
        total_owed_by_client = {
            row["client_id"]: row["total_owed"]
            for row in pending_qs.values("client_id").annotate(
                total_owed=Coalesce(Sum("total_amount"), cls.ZERO, output_field=cls.DECIMAL_FIELD)
            )
        }

        # ── Overdue bucket ────────────────────────────────────────────────
        overdue_rows = list(
            pending_qs.filter(due_date__lt=date_from)
            .values("client_id", "client__name")
            .annotate(
                day_total=Coalesce(Sum("total_amount"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                day_count=Count("id"),
                sample_invoice_id=Min("id"),
                has_proforma=BoolOr(cls.IS_PROFORMA),
            )
            .order_by("client__name")
        )
        overdue = cls._build_bucket(overdue_rows, total_owed_by_client)

        # ── Date buckets: one row per calendar day, even with no invoices ──
        window_rows = list(
            pending_qs.filter(due_date__gte=date_from, due_date__lte=date_to)
            .values("due_date", "client_id", "client__name")
            .annotate(
                day_total=Coalesce(Sum("total_amount"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
                day_count=Count("id"),
                sample_invoice_id=Min("id"),
                has_proforma=BoolOr(cls.IS_PROFORMA),
            )
            .order_by("due_date", "client__name")
        )
        rows_by_date = {
            due_date: list(rows)
            for due_date, rows in groupby(window_rows, key=lambda r: r["due_date"])
        }

        days_in_range = (date_to - date_from).days
        buckets = [
            {
                "date": date_from + timedelta(days=offset),
                **cls._build_bucket(rows_by_date.get(date_from + timedelta(days=offset), []), total_owed_by_client),
            }
            for offset in range(days_in_range + 1)
        ]

        window_total = sum((b["total"] for b in buckets), Decimal("0.00"))
        window_count = sum(b["count"] for b in buckets)

        # ── Month total: everything due this calendar month ────────────────
        month_start = today.replace(day=1)
        next_month_start = (month_start + timedelta(days=32)).replace(day=1)
        month_agg = pending_qs.filter(
            due_date__gte=month_start, due_date__lt=next_month_start
        ).aggregate(
            total=Coalesce(Sum("total_amount"), cls.ZERO, output_field=cls.DECIMAL_FIELD),
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
    def _build_bucket(rows, total_owed_by_client) -> dict:
        """Turns a list of already client-aggregated rows into one bucket:
        its own total/count, plus the per-client breakdown. view_url is
        computed once here (via Client.invoices_admin_url) so the calendar,
        its email report, and their templates all share the same link --
        no separate URL-building logic anywhere else."""
        clients = [
            {
                "client_id": row["client_id"],
                "client_name": row["client__name"],
                "day_total": row["day_total"],
                "day_count": row["day_count"],
                "total_owed": total_owed_by_client.get(row["client_id"], row["day_total"]),
                "sample_invoice_id": row["sample_invoice_id"],
                "view_url": Client(name=row["client__name"]).invoices_admin_url(),
                # True when at least one invoice in this row is still a pro
                # forma -- shown as a small badge in the calendar so the user
                # knows this amount isn't fully invoiced yet.
                "has_proforma": row["has_proforma"],
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
            "today": timezone.now().date(),
            "date_from": date_from,
            "date_to": date_to,
        }
