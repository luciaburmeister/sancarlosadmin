from decimal import Decimal

from django.db.models import Sum, Count
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from sales.models import Quote, QuoteStatus


class QuoteDashboardView(UnfoldModelAdminViewMixin, TemplateView):
    title = _("Quote Dashboard")
    permission_required = ("sales.can_view_quote_dashboard",)
    template_name = "admin/sales/quote/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        quotes = Quote.objects.all()

        # ── Global KPIs ─────────────────────────────────────────
        totals = quotes.aggregate(
            total_subtotal=Sum("subtotal_price"),
            total_tax=Sum("tax_amount"),
            total_price=Sum("total_price"),
            count=Count("id"),
        )

        ctx["kpi"] = {
            "total_quoted": totals["total_price"] or Decimal("0.00"),
            "total_subtotal": totals["total_subtotal"] or Decimal("0.00"),
            "total_tax": totals["total_tax"] or Decimal("0.00"),
            "count": totals["count"],
        }

        # ── Per-status breakdown (for the table) ─────────────────
        status_data = []
        status_colors = {
            QuoteStatus.DRAFT: {"bg": "#6366f1", "label": str(_("Draft"))},
            QuoteStatus.SENT: {"bg": "#3b82f6", "label": str(_("Sent"))},
            QuoteStatus.ACCEPTED: {"bg": "#10b981", "label": str(_("Accepted"))},
            QuoteStatus.REJECTED: {"bg": "#ef4444", "label": str(_("Rejected"))},
        }

        for status_value, meta in status_colors.items():
            agg = quotes.filter(status=status_value).aggregate(
                total=Sum("total_price"),
                subtotal=Sum("subtotal_price"),
                tax=Sum("tax_amount"),
                count=Count("id"),
            )
            status_data.append({
                "status": status_value,
                "label": meta["label"],
                "color": meta["bg"],
                "total": float(agg["total"] or 0),
                "subtotal": float(agg["subtotal"] or 0),
                "tax": float(agg["tax"] or 0),
                "count": agg["count"],
            })

        ctx["status_data"] = status_data

        return ctx
