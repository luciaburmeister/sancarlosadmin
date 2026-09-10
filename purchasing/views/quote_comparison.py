from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from purchasing.models import QuoteRequest, ProviderQuote, QuoteRequestStatus


class QuoteComparisonView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Side-by-side comparison of every ProviderQuote received for a
    QuoteRequest, rendered as one card per provider. Highlights the
    cheapest total and lets the user pick a winning quote to convert
    into a PurchaseOrder.
    """
    template_name = "purchasing/quote_comparison.html"
    title = _("Compare Quotes")
    permission_required = ("purchasing.change_quoterequest",)

    def get_quote_request(self):
        return get_object_or_404(QuoteRequest, pk=self.kwargs["quote_request_id"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quote_request = self.get_quote_request()
        quotes = list(
            quote_request.provider_quotes
            .select_related("provider")
            .prefetch_related("items__quote_request_item")
        )

        best_total = None
        for quote in quotes:
            if quote.total_amount and quote.total_amount.amount and (
                best_total is None or quote.total_amount.amount < best_total
            ):
                best_total = quote.total_amount.amount

        cards = []
        for quote in quotes:
            lines = [
                {
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit": line.get_unit_display(),
                    "unit_price": line.unit_price,
                    "line_total": line.line_total,
                }
                for line in quote.items.all()
            ]
            cards.append({
                "quote": quote,
                "lines": lines,
                "is_best_total": bool(
                    quote.total_amount and quote.total_amount.amount and
                    quote.total_amount.amount == best_total
                ),
                "can_select": (
                    quote_request.status != QuoteRequestStatus.CONVERTED and quote.is_complete
                ),
            })

        context.update({
            "quote_request": quote_request,
            "cards": cards,
            "opts": QuoteRequest._meta,
        })
        return context

    def post(self, request, *args, **kwargs):
        quote_request = self.get_quote_request()
        provider_quote = get_object_or_404(
            ProviderQuote,
            pk=request.POST.get("provider_quote_id"),
            quote_request=quote_request,
        )
        try:
            po = provider_quote.convert_to_purchase_order(user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect(request.path)

        messages.success(
            request,
            _("Purchase Order %(number)s created from the selected quote.") % {"number": po.number},
        )
        return redirect("admin:purchasing_purchaseorder_change", po.pk)