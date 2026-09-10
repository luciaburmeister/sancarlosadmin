from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView
from unfold.views import UnfoldModelAdminViewMixin

from purchasing.models import QuoteRequest, QuoteRequestStatus, ProviderQuote, ProviderQuoteItem, ProviderQuoteStatus
from purchasing.forms import CreateQuotesForm


class CreateQuotesView(UnfoldModelAdminViewMixin, FormView):
    """
    Lets the user pick one or more providers for this QuoteRequest. For
    every provider selected, creates a ProviderQuote (with its lines
    copied from the QuoteRequestItem lines) if one doesn't already exist
    — providers that already have a quote are simply skipped, so picking
    the same provider twice is harmless.

    Creating a ProviderQuote here does NOT send anything by itself: it
    only generates the record and its public link. Sending it is a
    separate, explicit step — either by email (Send Email button) or by
    copying the link and sharing it manually (e.g. WhatsApp).
    """
    template_name = "purchasing/create_quotes.html"
    title = _("Create Quotes")
    form_class = CreateQuotesForm
    permission_required = ("purchasing.add_providerquote",)

    def get_quote_request(self):
        return get_object_or_404(QuoteRequest, pk=self.kwargs["quote_request_id"])

    def dispatch(self, request, *args, **kwargs):
        quote_request = self.get_quote_request()
        if quote_request.status == QuoteRequestStatus.CONVERTED:
            messages.error(request, _("This quote request has already been converted to a Purchase Order."))
            return redirect("admin:purchasing_quoterequest_change", quote_request.pk)
        if not quote_request.items.exists():
            messages.error(request, _("Add at least one item before creating quotes."))
            return redirect("admin:purchasing_quoterequest_change", quote_request.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quote_request = self.get_quote_request()
        context.update({
            "quote_request": quote_request,
            "opts": QuoteRequest._meta,
            "existing_quotes": quote_request.provider_quotes.select_related("provider"),
        })
        return context

    def post(self, request, *args, **kwargs):
        quote_request = self.get_quote_request()

        send_email_quote_id = request.POST.get("send_email_quote_id")
        if send_email_quote_id:
            quote = get_object_or_404(ProviderQuote, pk=send_email_quote_id, quote_request=quote_request)
            try:
                quote.queue_quote_email(user=request.user)
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
            else:
                messages.success(request, _("Email queued to %(email)s.") % {"email": quote.provider.email})
            return redirect(reverse("admin:purchasing_quoterequest_create_quotes", args=[quote_request.pk]))

        delete_quote_id = request.POST.get("delete_quote_id")
        if delete_quote_id:
            quote = get_object_or_404(ProviderQuote, pk=delete_quote_id, quote_request=quote_request)
            if quote.status not in [ProviderQuoteStatus.PENDING, ProviderQuoteStatus.SENT]:
                messages.error(
                    request,
                    _("Only quotes that are still Pending or Sent can be deleted."),
                )
            else:
                quote.delete()
                messages.success(request, _("Quote deleted."))
            return redirect(reverse("admin:purchasing_quoterequest_create_quotes", args=[quote_request.pk]))
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        quote_request = self.get_quote_request()
        created_count = 0
        skipped_count = 0

        for provider in form.cleaned_data["providers"]:
            provider_quote, was_created = ProviderQuote.objects.get_or_create(
                quote_request=quote_request,
                provider=provider,
            )
            if was_created:
                created_count += 1
                for line in quote_request.items.all():
                    ProviderQuoteItem.objects.create(
                        provider_quote=provider_quote,
                        quote_request_item=line,
                        description=line.description,
                        quantity=line.quantity,
                        unit=line.unit,
                    )
            else:
                skipped_count += 1

        if created_count:
            if quote_request.status == QuoteRequestStatus.DRAFT:
                quote_request.status = QuoteRequestStatus.REQUESTED
                quote_request.save(update_fields=["status"])
            messages.success(
                self.request,
                _("%(count)d quote(s) created. Copy each link and send it to the provider.")
                % {"count": created_count},
            )
        if skipped_count:
            messages.info(
                self.request,
                _("%(count)d provider(s) already had a quote for this request and were skipped.")
                % {"count": skipped_count},
            )
        if not created_count and not skipped_count:
            messages.info(self.request, _("No providers were selected."))

        return redirect(reverse("admin:purchasing_quoterequest_create_quotes", args=[quote_request.pk]))