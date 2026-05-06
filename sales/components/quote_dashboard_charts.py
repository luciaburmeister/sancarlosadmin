import json

from unfold.components import BaseComponent, register_component


@register_component
class QuoteValueByStatusChart(BaseComponent):
    """Bar chart showing total quoted value broken down by status."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from sales.models import Quote, QuoteStatus
        from django.db.models import Sum, Count
        from django.utils.translation import gettext as _

        status_meta = [
            (QuoteStatus.DRAFT, _("Draft"), "#6366f1"),
            (QuoteStatus.SENT, _("Sent"), "#3b82f6"),
            (QuoteStatus.ACCEPTED, _("Accepted"), "#10b981"),
            (QuoteStatus.REJECTED, _("Rejected"), "#ef4444"),
        ]

        labels = []
        totals = []
        colors = []

        for status_value, label, color in status_meta:
            agg = Quote.objects.filter(status=status_value).aggregate(
                total=Sum("total_price"),
            )
            labels.append(str(label))
            totals.append(float(agg["total"] or 0))
            colors.append(color)

        context.update({
            "height": 320,
            "data": json.dumps({
                "labels": labels,
                "datasets": [{
                    "label": str(_("Total Quoted (€)")),
                    "data": totals,
                    "backgroundColor": colors,
                    "displayYAxis": True,
                    "suffixYAxis": " €",
                }],
            }),
        })
        return context


@register_component
class QuoteCountByStatusChart(BaseComponent):
    """Bar chart showing number of quotes broken down by status."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from sales.models import Quote, QuoteStatus
        from django.db.models import Count
        from django.utils.translation import gettext as _

        status_meta = [
            (QuoteStatus.DRAFT, _("Draft"), "#6366f1"),
            (QuoteStatus.SENT, _("Sent"), "#3b82f6"),
            (QuoteStatus.ACCEPTED, _("Accepted"), "#10b981"),
            (QuoteStatus.REJECTED, _("Rejected"), "#ef4444"),
        ]

        labels = []
        counts = []
        colors = []

        for status_value, label, color in status_meta:
            count = Quote.objects.filter(status=status_value).count()
            labels.append(str(label))
            counts.append(count)
            colors.append(color)

        context.update({
            "height": 320,
            "data": json.dumps({
                "labels": labels,
                "datasets": [{
                    "label": str(_("# Quotes")),
                    "data": counts,
                    "backgroundColor": colors,
                    "displayYAxis": True,
                }],
            }),
        })
        return context
