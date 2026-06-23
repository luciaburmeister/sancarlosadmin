from datetime import date, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db.models import (
    DecimalField,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from invoices.models import Invoice, InvoicePaymentStatus
from payments.models import PaymentAllocation
from payroll.services.cashflow import build_week_ranges, get_payroll_outflows


# Cache TTL in seconds (5 minutes)
_CACHE_TTL = 300


class AccountsPayableDashboardView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Dashboard view for Accounts Payable (Cuentas por Pagar).
    Shows outstanding purchase invoices with KPIs, aging analysis,
    cash flow projection, and a detailed invoice table.

    All heavy aggregations run at the database level and results are
    cached per company + date-range for 5 minutes.
    """

    title = _("Accounts Payable")
    permission_required = ("invoices.accounts_payable",)
    template_name = "admin/invoices/invoice/accounts_payable.html"
    model_admin = None

    # ── Queryset helpers ───────────────────────────────────────────

    @staticmethod
    def _invoice_ct_id():
        """Return the ContentType id for Invoice (cached after first call)."""
        return ContentType.objects.get_for_model(Invoice).pk

    @staticmethod
    def _annotate_remaining(qs):
        """
        Annotate a queryset with `db_amount_paid` and `db_amount_remaining`
        computed at the database level via PaymentAllocation.
        Eliminates the N+1 caused by the model's Python properties.
        """
        ct_id = AccountsPayableDashboardView._invoice_ct_id()

        paid_subquery = Subquery(
            PaymentAllocation.objects.filter(
                content_type_id=ct_id,
                object_id=OuterRef("pk"),
            )
            .values("object_id")
            .annotate(total=Sum("amount_applied"))
            .values("total"),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )

        return qs.annotate(
            db_amount_paid=Coalesce(
                paid_subquery,
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
            db_amount_remaining=Coalesce(
                F("total_amount") - Coalesce(
                    paid_subquery,
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )

    # ── Main view ──────────────────────────────────────────────────

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        # ── Company filtering ──────────────────────────────────────
        user_companies = get_user_companies(request.user)
        selected_company_id = request.GET.get("company")

        try:
            selected_company_id = int(selected_company_id) if selected_company_id else None
        except (ValueError, TypeError):
            selected_company_id = None

        if selected_company_id and user_companies.filter(id=selected_company_id).exists():
            selected_company = user_companies.get(id=selected_company_id)
        elif user_companies.exists():
            selected_company = user_companies.first()
        else:
            selected_company = None

        # ── Date range for cash flow ───────────────────────────────
        today = timezone.now().date()
        default_from = today
        default_to = today + timedelta(days=90)

        date_from_str = request.GET.get("date_from", "")
        date_to_str = request.GET.get("date_to", "")

        try:
            cf_date_from = date.fromisoformat(date_from_str) if date_from_str else default_from
        except ValueError:
            cf_date_from = default_from

        try:
            cf_date_to = date.fromisoformat(date_to_str) if date_to_str else default_to
        except ValueError:
            cf_date_to = default_to

        if cf_date_from > cf_date_to:
            cf_date_from, cf_date_to = cf_date_to, cf_date_from

        # ── Base querysets ─────────────────────────────────────────
        if selected_company:
            base_qs = Invoice.objects.filter(company=selected_company)
        else:
            base_qs = Invoice.objects.none()

        unpaid_qs = base_qs.exclude(payment_status=InvoicePaymentStatus.PAID)

        # ── Cache key ─────────────────────────────────────────────
        company_pk = selected_company.pk if selected_company else 0
        cache_key = (
            f"ap_dash:{company_pk}:{cf_date_from.isoformat()}:{cf_date_to.isoformat()}"
        )
        cached = cache.get(cache_key)

        if cached:
            kpis, aging, cash_flow = cached
        else:
            # Annotate the unpaid queryset once for aggregations
            annotated_unpaid = self._annotate_remaining(unpaid_qs)

            # Shared weekly buckets so invoice and payroll projections align
            weeks = build_week_ranges(cf_date_from, cf_date_to)
            company_ids = [selected_company.id] if selected_company else []

            kpis = self._compute_kpis(annotated_unpaid, base_qs, today)
            aging = self._compute_aging(annotated_unpaid, today)
            cash_flow = self._compute_cash_flow(annotated_unpaid, weeks, cf_date_from)

            payroll_out = get_payroll_outflows(
                company_ids, cf_date_from, cf_date_to, weeks
            )
            # Align payroll outflows to the same weeks for a stacked chart series
            cash_flow["payroll_weeks"] = [w["amount"] for w in payroll_out["weeks"]]
            kpis["payroll_total"] = payroll_out["total"]
            kpis["payroll_net"] = payroll_out["net_total"]
            kpis["payroll_ss"] = payroll_out["ss_total"]

            cache.set(cache_key, (kpis, aging, cash_flow), _CACHE_TTL)

        # ── Invoice table queryset (annotated for template) ───────
        invoices_qs = (
            self._annotate_remaining(unpaid_qs)
            .select_related("provider", "company")
            .order_by("due_date", "-date_issued")
        )

        # ── Populate context ──────────────────────────────────────
        ctx.update({
            "title": _("Accounts Payable"),
            "opts": Invoice._meta,
            "companies": user_companies,
            "selected_company": selected_company,
            "kpis": kpis,
            "aging": aging,
            "cash_flow": cash_flow,
            "cash_flow_json": cash_flow,
            "invoices": invoices_qs,
            "today": today,
            "cf_date_from": cf_date_from.isoformat(),
            "cf_date_to": cf_date_to.isoformat(),
        })
        return ctx

    # ── Private helpers (DB-level) ─────────────────────────────────

    @staticmethod
    def _compute_kpis(annotated_unpaid, base_qs, today):
        """Compute the four main KPI values via database aggregation."""
        agg = annotated_unpaid.aggregate(
            total_payable=Coalesce(
                Sum("db_amount_remaining"),
                Value(Decimal("0.00")),
                output_field=DecimalField(),
            ),
            total_overdue=Coalesce(
                Sum(
                    "db_amount_remaining",
                    filter=Q(due_date__lt=today),
                ),
                Value(Decimal("0.00")),
                output_field=DecimalField(),
            ),
        )

        # Paid this month – simple aggregate on total_amount
        month_start = today.replace(day=1)
        paid_agg = base_qs.filter(
            payment_status=InvoicePaymentStatus.PAID,
            date_issued__gte=month_start,
        ).aggregate(
            paid=Coalesce(
                Sum("total_amount"),
                Value(Decimal("0.00")),
                output_field=DecimalField(),
            ),
        )

        return {
            "total_payable": agg["total_payable"],
            "total_overdue": agg["total_overdue"],
            "paid_this_month": paid_agg["paid"],
            "pending_count": annotated_unpaid.count(),
        }

    @staticmethod
    def _compute_aging(annotated_unpaid, today):
        """Compute aging buckets via a single aggregate query with conditional Sum."""
        d30 = today - timedelta(days=30)
        d60 = today - timedelta(days=60)
        d90 = today - timedelta(days=90)

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        agg = annotated_unpaid.aggregate(
            current=Coalesce(
                Sum("db_amount_remaining", filter=Q(due_date__gte=today)),
                zero, output_field=field,
            ),
            days_1_30=Coalesce(
                Sum("db_amount_remaining", filter=Q(due_date__lt=today, due_date__gte=d30)),
                zero, output_field=field,
            ),
            days_31_60=Coalesce(
                Sum("db_amount_remaining", filter=Q(due_date__lt=d30, due_date__gte=d60)),
                zero, output_field=field,
            ),
            days_61_90=Coalesce(
                Sum("db_amount_remaining", filter=Q(due_date__lt=d60, due_date__gte=d90)),
                zero, output_field=field,
            ),
            days_90_plus=Coalesce(
                Sum("db_amount_remaining", filter=Q(due_date__lt=d90)),
                zero, output_field=field,
            ),
            no_due_date=Coalesce(
                Sum("db_amount_remaining", filter=Q(due_date__isnull=True)),
                zero, output_field=field,
            ),
        )

        total = sum(agg.values())
        return {
            key: {
                "amount": value,
                "percentage": (
                    float((value / total * 100).quantize(Decimal("0.1")))
                    if total > 0
                    else 0.0
                ),
            }
            for key, value in agg.items()
        }

    @staticmethod
    def _compute_cash_flow(annotated_unpaid, weeks, date_from):
        """Compute weekly cash-flow projection via database aggregation.

        ``weeks`` are prebuilt windows (see ``build_week_ranges``) so this
        projection lines up with the payroll one shown on the same chart.
        """
        # Build a single aggregate with one conditional Sum per week
        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        agg_kwargs = {}
        for idx, w in enumerate(weeks):
            agg_kwargs[f"week_{idx}"] = Coalesce(
                Sum(
                    "db_amount_remaining",
                    filter=Q(due_date__gte=w["start"], due_date__lte=w["end"]),
                ),
                zero,
                output_field=field,
            )

        # Overdue total: due before date_from
        agg_kwargs["overdue_total"] = Coalesce(
            Sum("db_amount_remaining", filter=Q(due_date__lt=date_from)),
            zero,
            output_field=field,
        )

        agg = annotated_unpaid.aggregate(**agg_kwargs)

        return {
            "weeks": [
                {"label": w["label"], "amount": float(agg[f"week_{idx}"])}
                for idx, w in enumerate(weeks)
            ],
            "overdue_total": float(agg["overdue_total"]),
        }
