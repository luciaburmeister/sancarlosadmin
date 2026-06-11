from datetime import date, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db.models import (
    DecimalField,
    Sum,
    Value,
    Count,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from invoices.models import AppliedTax, Invoice, InvoicePaymentStatus, SalesInvoice, SalesInvoiceStatus

from production.models import (
            ProductionTicket,
            ProductionItemSession,
            ProductionItemSessionStatus,
        )
from configurator.models import Configuration

_CACHE_TTL = 300


class OperationalDashboardView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Operational dashboard:
    1. Invoicing without taxes — SalesInvoice.subtotal_amount, split pro-forma / final
    2. Production costs — Configuration.total_cost for tickets worked in the period
    """

    title = _("Operational Dashboard")
    permission_required = ("invoices.operational_dashboard",)
    template_name = "admin/invoices/invoice/operational_dashboard.html"
    model_admin = None

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

        # ── Date range ─────────────────────────────────────────────
        today = timezone.now().date()
        default_from = today.replace(day=1)
        default_to = today

        date_from_str = request.GET.get("date_from", "")
        date_to_str = request.GET.get("date_to", "")

        try:
            date_from = date.fromisoformat(date_from_str) if date_from_str else default_from
        except ValueError:
            date_from = default_from

        try:
            date_to = date.fromisoformat(date_to_str) if date_to_str else default_to
        except ValueError:
            date_to = default_to

        if date_from > date_to:
            date_from, date_to = date_to, date_from

        # ── Cache ──────────────────────────────────────────────────
        company_pk = selected_company.pk if selected_company else 0
        cache_key = f"op_dash:{company_pk}:{date_from.isoformat()}:{date_to.isoformat()}"
        cached = cache.get(cache_key)

        if cached:
            invoicing_data, production_data, tax_data, payment_data = cached
        else:
            invoicing_data = self._compute_invoicing(selected_company, date_from, date_to)
            production_data = self._compute_production_costs(selected_company, date_from, date_to)
            tax_data = self._compute_tax_summary(selected_company, date_from, date_to)
            payment_data = self._compute_payment_invoicing(selected_company, date_from, date_to)
            cache.set(cache_key, (invoicing_data, production_data, tax_data, payment_data), _CACHE_TTL)

        # Tax periods are NOT cached — always fresh so status changes reflect immediately
        tax_periods = self._compute_tax_periods(selected_company, date_from, date_to)

        ctx.update({
            "title": _("Operational Dashboard"),
            "opts": SalesInvoice._meta,
            "companies": user_companies,
            "selected_company": selected_company,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "invoicing": invoicing_data,
            "production": production_data,
            "taxes": tax_data,
            "payment": payment_data,
            "tax_periods": tax_periods,
            "today": today,
        })
        return ctx

    # ── Invoicing helpers ──────────────────────────────────────────

    @staticmethod
    def _compute_invoicing(company, date_from, date_to):
        if not company:
            return {
                "final_total": Decimal("0.00"),
                "final_count": 0,
                "proforma_total": Decimal("0.00"),
                "proforma_count": 0,
                "grand_total": Decimal("0.00"),
                "monthly_final": [],
                "monthly_proforma": [],
                "top_clients": [],
            }

        base_qs = SalesInvoice.objects.filter(
            company=company,
            date_issued__gte=date_from,
            date_issued__lte=date_to,
        )

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        # KPIs
        final_agg = base_qs.filter(status=SalesInvoiceStatus.FINAL).aggregate(
            total=Coalesce(Sum("subtotal_amount"), zero, output_field=field),
            count=Count("id"),
        )
        proforma_agg = base_qs.filter(status=SalesInvoiceStatus.PROFORMA).aggregate(
            total=Coalesce(Sum("subtotal_amount"), zero, output_field=field),
            count=Count("id"),
        )

        # Monthly breakdown
        monthly_final = list(
            base_qs.filter(status=SalesInvoiceStatus.FINAL)
            .annotate(month=TruncMonth("date_issued"))
            .values("month")
            .annotate(total=Coalesce(Sum("subtotal_amount"), zero, output_field=field))
            .order_by("month")
        )

        monthly_proforma = list(
            base_qs.filter(status=SalesInvoiceStatus.PROFORMA)
            .annotate(month=TruncMonth("date_issued"))
            .values("month")
            .annotate(total=Coalesce(Sum("subtotal_amount"), zero, output_field=field))
            .order_by("month")
        )

        # Top clients (subtotal without tax)
        top_clients = list(
            base_qs.values("client__name")
            .annotate(total=Coalesce(Sum("subtotal_amount"), zero, output_field=field))
            .order_by("-total")[:10]
        )

        # Pending to collect: total_amount (with taxes) for FINAL invoices
        # minus total payments allocated to those invoices
        final_qs = base_qs.filter(status=SalesInvoiceStatus.FINAL)
        final_total_with_tax = final_qs.aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
        )["total"]

        sales_ct = ContentType.objects.get_for_model(SalesInvoice)
        final_ids = list(final_qs.values_list("pk", flat=True))

        from payments.models import PaymentAllocation
        total_paid = PaymentAllocation.objects.filter(
            content_type=sales_ct,
            object_id__in=final_ids,
        ).aggregate(
            total=Coalesce(Sum("amount_applied"), zero, output_field=field),
        )["total"]

        pending_to_collect = final_total_with_tax - total_paid

        # Cumulative debt: all FINAL invoices issued up to date_to that
        # still have outstanding balance (regardless of date_from)
        all_final_qs = SalesInvoice.objects.filter(
            company=company,
            status=SalesInvoiceStatus.FINAL,
            date_issued__lte=date_to,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)

        cumulative_total = all_final_qs.aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
        )["total"]

        cumulative_ids = list(all_final_qs.values_list("pk", flat=True))
        cumulative_paid = PaymentAllocation.objects.filter(
            content_type=sales_ct,
            object_id__in=cumulative_ids,
        ).aggregate(
            total=Coalesce(Sum("amount_applied"), zero, output_field=field),
        )["total"]

        cumulative_debt = cumulative_total - cumulative_paid

        return {
            "final_total": final_agg["total"],
            "final_count": final_agg["count"],
            "proforma_total": proforma_agg["total"],
            "proforma_count": proforma_agg["count"],
            "grand_total": final_agg["total"] + proforma_agg["total"],
            "monthly_final": monthly_final,
            "monthly_proforma": monthly_proforma,
            "top_clients": top_clients,
            "final_total_with_tax": final_total_with_tax,
            "total_paid": total_paid,
            "pending_to_collect": pending_to_collect,
            "cumulative_debt": cumulative_debt,
        }

    # ── Production cost helpers ─────────────────────────────────────

    @staticmethod
    def _compute_production_costs(company, date_from, date_to):
        if not company:
            return {
                "total_cost": Decimal("0.00"),
                "ticket_count": 0,
                "monthly": [],
            }

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        config_ct = ContentType.objects.get_for_model(Configuration)

        # Find tickets that had sessions completed in this period
        session_qs = ProductionItemSession.objects.filter(
            company=company,
            status=ProductionItemSessionStatus.DONE,
            start_time__date__gte=date_from,
            start_time__date__lte=date_to,
        )

        ticket_ids_with_work = set(
            session_qs.values_list(
                "production_item__ticket_id", flat=True
            )
        )

        # Get distinct config IDs from tickets that point to a Configuration
        config_ids = list(
            ProductionTicket.objects.filter(
                pk__in=ticket_ids_with_work,
                company=company,
                content_type=config_ct,
            ).values_list("object_id", flat=True).distinct()
        )

        # DB-level aggregate on the underlying amount column
        total_cost = Configuration.objects.filter(
            pk__in=config_ids,
        ).aggregate(
            total=Coalesce(Sum("total_cost"), zero, output_field=field),
        )["total"]

        # Monthly cost breakdown
        monthly_data = []
        sessions_by_month = (
            session_qs
            .annotate(month=TruncMonth("start_time"))
            .values("month")
            .annotate(
                ticket_count=Count("production_item__ticket_id", distinct=True),
            )
            .order_by("month")
        )

        for row in sessions_by_month:
            month = row["month"]
            month_ticket_ids = set(
                session_qs.filter(
                    start_time__year=month.year,
                    start_time__month=month.month,
                ).values_list("production_item__ticket_id", flat=True)
            )
            month_config_ids = list(
                ProductionTicket.objects.filter(
                    pk__in=month_ticket_ids,
                    content_type=config_ct,
                ).values_list("object_id", flat=True).distinct()
            )

            month_cost = Configuration.objects.filter(
                pk__in=month_config_ids,
            ).aggregate(
                total=Coalesce(Sum("total_cost"), zero, output_field=field),
            )["total"]

            monthly_data.append({
                "month": month,
                "total": month_cost,
            })

        ticket_count = len(ticket_ids_with_work)
        avg_cost = (total_cost / ticket_count).quantize(Decimal("0.01")) if ticket_count else Decimal("0.00")

        return {
            "total_cost": total_cost,
            "ticket_count": ticket_count,
            "avg_cost_per_ticket": avg_cost,
            "monthly": monthly_data,
        }

    # ── Tax summary helpers ─────────────────────────────────────────

    @staticmethod
    def _compute_tax_summary(company, date_from, date_to):
        """Aggregate AppliedTax by tax_type for sales (billed) and purchase (paid) invoices."""
        if not company:
            return {
                "billed": [],
                "paid": [],
                "billed_total": Decimal("0.00"),
                "paid_total": Decimal("0.00"),
                "net": Decimal("0.00"),
            }

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        sales_ct = ContentType.objects.get_for_model(SalesInvoice)
        purchase_ct = ContentType.objects.get_for_model(Invoice)

        # ── Taxes billed (on SalesInvoices in the period) ──────────
        sales_invoice_ids = list(
            SalesInvoice.objects.filter(
                company=company,
                date_issued__gte=date_from,
                date_issued__lte=date_to,
            ).values_list("pk", flat=True)
        )

        billed_qs = (
            AppliedTax.objects.filter(
                content_type=sales_ct,
                object_id__in=sales_invoice_ids,
            )
            .values("tax_type__tax_name")
            .annotate(
                total_base=Coalesce(Sum("base_amount"), zero, output_field=field),
                total_tax=Coalesce(Sum("calculated_amount"), zero, output_field=field),
                count=Count("id"),
            )
            .order_by("-total_tax")
        )

        billed = list(billed_qs)
        billed_total = sum(row["total_tax"] for row in billed)

        # ── Taxes paid (on purchase Invoices in the period) ────────
        purchase_invoice_ids = list(
            Invoice.objects.filter(
                company=company,
                date_issued__gte=date_from,
                date_issued__lte=date_to,
            ).values_list("pk", flat=True)
        )

        paid_qs = (
            AppliedTax.objects.filter(
                content_type=purchase_ct,
                object_id__in=purchase_invoice_ids,
            )
            .values("tax_type__tax_name")
            .annotate(
                total_base=Coalesce(Sum("base_amount"), zero, output_field=field),
                total_tax=Coalesce(Sum("calculated_amount"), zero, output_field=field),
                count=Count("id"),
            )
            .order_by("-total_tax")
        )

        paid = list(paid_qs)
        paid_total = sum(row["total_tax"] for row in paid)

        # ── Per-tax balance (billed vs paid, net per tax type) ─────
        balance_map = {}  # key: tax_name → {billed, paid, net}

        for row in billed:
            key = row["tax_type__tax_name"]
            balance_map.setdefault(key, {
                "tax_name": key,
                "billed_base": Decimal("0.00"),
                "billed_tax": Decimal("0.00"),
                "paid_base": Decimal("0.00"),
                "paid_tax": Decimal("0.00"),
            })
            balance_map[key]["billed_base"] += row["total_base"]
            balance_map[key]["billed_tax"] += row["total_tax"]

        for row in paid:
            key = row["tax_type__tax_name"]
            balance_map.setdefault(key, {
                "tax_name": key,
                "billed_base": Decimal("0.00"),
                "billed_tax": Decimal("0.00"),
                "paid_base": Decimal("0.00"),
                "paid_tax": Decimal("0.00"),
            })
            balance_map[key]["paid_base"] += row["total_base"]
            balance_map[key]["paid_tax"] += row["total_tax"]

        balance = []
        for entry in balance_map.values():
            entry["net"] = entry["billed_tax"] - entry["paid_tax"]
            balance.append(entry)

        # Sort by absolute net descending so the biggest positions come first
        balance.sort(key=lambda x: abs(x["net"]), reverse=True)

        return {
            "billed": billed,
            "paid": paid,
            "billed_total": billed_total,
            "paid_total": paid_total,
            "net": billed_total - paid_total,
            "balance": balance,
        }

    # ── Payment invoicing helpers ───────────────────────────────────

    @staticmethod
    def _compute_payment_invoicing(company, date_from, date_to):
        """Aggregate purchase invoices with due_date in the selected range."""
        if not company:
            return {
                "total": Decimal("0.00"),
                "count": 0,
                "paid_total": Decimal("0.00"),
                "paid_count": 0,
                "unpaid_total": Decimal("0.00"),
                "unpaid_count": 0,
                "monthly": [],
                "top_providers": [],
            }

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        base_qs = Invoice.objects.filter(
            company=company,
            due_date__gte=date_from,
            due_date__lte=date_to,
        )

        # Overall totals
        total_agg = base_qs.aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        # By payment status
        paid_agg = base_qs.filter(
            payment_status=InvoicePaymentStatus.PAID,
        ).aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        unpaid_agg = base_qs.exclude(
            payment_status=InvoicePaymentStatus.PAID,
        ).aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        # Monthly breakdown by due_date
        monthly = list(
            base_qs
            .annotate(month=TruncMonth("due_date"))
            .values("month")
            .annotate(total=Coalesce(Sum("total_amount"), zero, output_field=field))
            .order_by("month")
        )

        # Top providers (by total amount due in period)
        top_providers = list(
            base_qs
            .values("provider__name")
            .annotate(total=Coalesce(Sum("total_amount"), zero, output_field=field))
            .order_by("-total")[:10]
        )

        # Outstanding providers: who we still owe money to (up to date_to)
        # All unpaid/partial invoices with due_date <= date_to
        outstanding_qs = Invoice.objects.filter(
            company=company,
            due_date__lte=date_to,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)

        outstanding_providers = list(
            outstanding_qs
            .values("provider__id", "provider__name")
            .annotate(
                total_owed=Coalesce(Sum("total_amount"), zero, output_field=field),
                invoice_count=Count("id"),
            )
            .order_by("-total_owed")[:15]
        )

        # Cumulative purchase debt: total owed across all unpaid invoices up to date_to
        cumulative_purchase_debt = outstanding_qs.aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        return {
            "total": total_agg["total"],
            "count": total_agg["count"],
            "paid_total": paid_agg["total"],
            "paid_count": paid_agg["count"],
            "unpaid_total": unpaid_agg["total"],
            "unpaid_count": unpaid_agg["count"],
            "monthly": monthly,
            "top_providers": top_providers,
            "outstanding_providers": outstanding_providers,
            "cumulative_debt": cumulative_purchase_debt["total"],
            "cumulative_debt_count": cumulative_purchase_debt["count"],
        }

    @staticmethod
    def _compute_tax_periods(company, date_from, date_to):
        """
        Find all TaxPeriods for this company that overlap with date_from→date_to,
        plus the immediately preceding period for each rule found.
        Returns a list of TaxPeriod objects ordered by date_from ascending.
        """
        if not company:
            return []

        from finance.models import TaxPeriod

        # Periods that overlap with the selected range:
        # overlap condition: period starts before date_to AND ends after date_from
        overlapping = TaxPeriod.objects.filter(
            company=company,
            date_from__lte=date_to,
            date_to__gte=date_from,
            rule__tax_type__tax_name__icontains="IVA",
        ).select_related("rule", "rule__tax_type").order_by("rule", "date_from")

        # Collect the previous period for each rule
        period_ids = set(overlapping.values_list("id", flat=True))
        rules_seen = set()

        for period in overlapping:
            rule = period.rule
            if rule.id in rules_seen:
                continue
            rules_seen.add(rule.id)

            # Find the period immediately before the earliest overlapping one for this rule
            earliest = overlapping.filter(rule=rule).order_by("date_from").first()
            if earliest:
                prev = (
                    TaxPeriod.objects.filter(
                        rule=rule,
                        date_to__lt=earliest.date_from,
                    )
                    .order_by("-date_from")
                    .first()
                )
                if prev:
                    period_ids.add(prev.id)

        return list(
            TaxPeriod.objects.filter(id__in=period_ids)
            .select_related("rule", "rule__tax_type")
            .order_by("rule__tax_type__tax_name", "date_from")
        )