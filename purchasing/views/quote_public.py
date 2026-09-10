from django.shortcuts import get_object_or_404, render
from django.views.generic import View

from purchasing.models import ProviderQuote, ProviderQuoteStatus
from purchasing.forms import QuotePublicResponseForm


class QuotePublicResponseView(View):
    """
    Public, non-authenticated page that a provider opens via a link
    containing their unique ProviderQuote token. They fill in the unit
    price for each requested line plus shipping/discount/validity/
    delivery time; nothing else in Polipus is reachable from this page.
    """
    template_name = "purchasing/quote_public_response.html"

    def get_provider_quote(self, token):
        return get_object_or_404(ProviderQuote, token=token)

    def get(self, request, token, *args, **kwargs):
        provider_quote = self.get_provider_quote(token)
        already_closed = provider_quote.status in [
            ProviderQuoteStatus.SELECTED,
            ProviderQuoteStatus.REJECTED,
        ]
        form = None if already_closed else QuotePublicResponseForm(provider_quote=provider_quote)
        return render(request, self.template_name, {
            "provider_quote": provider_quote,
            "form": form,
            "already_closed": already_closed,
            "submitted": False,
        })

    def post(self, request, token, *args, **kwargs):
        provider_quote = self.get_provider_quote(token)
        if provider_quote.status in [ProviderQuoteStatus.SELECTED, ProviderQuoteStatus.REJECTED]:
            return render(request, self.template_name, {
                "provider_quote": provider_quote,
                "form": None,
                "already_closed": True,
                "submitted": False,
            })

        form = QuotePublicResponseForm(request.POST, provider_quote=provider_quote)
        if form.is_valid():
            form.save()
            return render(request, self.template_name, {
                "provider_quote": provider_quote,
                "form": None,
                "already_closed": False,
                "submitted": True,
            })

        return render(request, self.template_name, {
            "provider_quote": provider_quote,
            "form": form,
            "already_closed": False,
            "submitted": False,
        })