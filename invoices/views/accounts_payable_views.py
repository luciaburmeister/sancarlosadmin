from datetime import date, timedelta
from decimal import Decimal


from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from core.admin.company_access import get_user_companies
from invoices.models import Invoice, InvoicePaymentStatus


class AccountsPayableDashboardView(TemplateView):
    """
    Dashboard view for Accounts Payable (Cuentas por Pagar).
    Shows outstanding purchase invoices with KPIs, aging analysis,
    cash flow projection, and a detailed invoice table.
    """

    template_name = "admin/invoices/invoice/accounts_payable.html"
    model_admin = None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        # ── Admin site context ─────────────────────────────────────
        if self.model_admin:
            ctx.update(self.model_admin.admin_site.each_context(request))

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

        # ── Base queryset ──────────────────────────────────────────
        base_qs = Invoice.objects.filter(company=selected_company) if selected_company else Invoice.objects.none()
        unpaid_qs = base_qs.exclude(payment_status=InvoicePaymentStatus.PAID)

        # ── Populate context ───────────────────────────────────────
        cash_flow = self._compute_cash_flow(unpaid_qs, cf_date_from, cf_date_to)
        ctx.update({
            "title": _("Accounts Payable"),
            "opts": Invoice._meta,
            "companies": user_companies,
            "selected_company": selected_company,
            "kpis": self._compute_kpis(unpaid_qs, base_qs, today),
            "aging": self._compute_aging(unpaid_qs, today),
            "cash_flow": cash_flow,
            "cash_flow_json": cash_flow,  # used by json_script in template
            "invoices": unpaid_qs.select_related("provider", "company").order_by("due_date", "-date_issued"),
            "today": today,
            "cf_date_from": cf_date_from.isoformat(),
            "cf_date_to": cf_date_to.isoformat(),
        })
        return ctx

    # ── Private helpers ────────────────────────────────────────────

    def _compute_kpis(self, unpaid_qs, base_qs, today):
        """Compute the four main KPI values."""
        total_payable = Decimal("0.00")
        total_overdue = Decimal("0.00")
        for inv in unpaid_qs:
            remaining = inv.amount_remaining
            amt = remaining.amount if hasattr(remaining, "amount") else Decimal(str(remaining))
            total_payable += amt
            if inv.due_date and inv.due_date < today:
                total_overdue += amt

        # Paid this month
        month_start = today.replace(day=1)
        paid_this_month = Decimal("0.00")
        for inv in base_qs.filter(payment_status=InvoicePaymentStatus.PAID, date_issued__gte=month_start):
            total = inv.total_amount
            paid_this_month += total.amount if hasattr(total, "amount") else Decimal(str(total))

        return {
            "total_payable": total_payable,
            "total_overdue": total_overdue,
            "paid_this_month": paid_this_month,
            "pending_count": unpaid_qs.count(),
        }

    def _compute_aging(self, unpaid_qs, today):
        """Compute aging buckets based on days past due date."""
        buckets = {
            "current": Decimal("0.00"),
            "days_1_30": Decimal("0.00"),
            "days_31_60": Decimal("0.00"),
            "days_61_90": Decimal("0.00"),
            "days_90_plus": Decimal("0.00"),
            "no_due_date": Decimal("0.00"),
        }

        for inv in unpaid_qs:
            remaining = inv.amount_remaining
            amt = remaining.amount if hasattr(remaining, "amount") else Decimal(str(remaining))

            if not inv.due_date:
                buckets["no_due_date"] += amt
            else:
                days_overdue = (today - inv.due_date).days
                if days_overdue <= 0:
                    buckets["current"] += amt
                elif days_overdue <= 30:
                    buckets["days_1_30"] += amt
                elif days_overdue <= 60:
                    buckets["days_31_60"] += amt
                elif days_overdue <= 90:
                    buckets["days_61_90"] += amt
                else:
                    buckets["days_90_plus"] += amt

        total = sum(buckets.values())
        return {
            key: {
                "amount": value,
                "percentage": float((value / total * 100).quantize(Decimal("0.1"))) if total > 0 else 0.0,
            }
            for key, value in buckets.items()
        }

    def _compute_cash_flow(self, unpaid_qs, date_from, date_to):
        """Compute weekly cash flow projection from due_date."""
        cf_invoices = unpaid_qs.filter(due_date__gte=date_from, due_date__lte=date_to)

        # Build weekly buckets
        weeks = []
        cursor = date_from
        while cursor <= date_to:
            week_end = min(cursor + timedelta(days=6), date_to)
            weeks.append({"start": cursor, "end": week_end, "label": cursor.strftime("%d %b"), "amount": Decimal("0.00")})
            cursor = week_end + timedelta(days=1)

        for inv in cf_invoices:
            remaining = inv.amount_remaining
            amt = remaining.amount if hasattr(remaining, "amount") else Decimal(str(remaining))
            for week in weeks:
                if week["start"] <= inv.due_date <= week["end"]:
                    week["amount"] += amt
                    break

        # Overdue total
        overdue_total = Decimal("0.00")
        for inv in unpaid_qs.filter(due_date__lt=date_from):
            remaining = inv.amount_remaining
            overdue_total += remaining.amount if hasattr(remaining, "amount") else Decimal(str(remaining))

        return {
            "weeks": [{"label": w["label"], "amount": float(w["amount"])} for w in weeks],
            "overdue_total": float(overdue_total),
        }
