"""Expandable row cards for the expense admin changelists."""
from django.db.models import Prefetch
from django.urls import reverse
from unfold.sections import TemplateSection

from apps.expenses.models import ExpenseTicket
from payments.mixins import currency_symbol

# Where get_queryset() prefetches each claim's tickets for the section.
CLAIM_TICKETS_ATTR = "claim_tickets"


def claim_tickets_prefetch() -> Prefetch:
    """The tickets behind a claim's total, ready for the section to render."""
    return Prefetch(
        "tickets",
        queryset=ExpenseTicket.objects.select_related("provider", "exchange_rate"),
        to_attr=CLAIM_TICKETS_ATTR,
    )


class ClaimTicketsSection(TemplateSection):
    """Expandable row card breaking a claim's total down ticket by ticket."""

    template_name = "expenses/claim_tickets_section.html"

    def get_context_data(self, request, instance):
        # The prefetch is what keeps this off N+1, but the card still has to be
        # right on a queryset that didn't set it up (a detail view, say).
        tickets = getattr(instance, CLAIM_TICKETS_ATTR, None)
        if tickets is None:
            tickets = instance.tickets.select_related("provider", "exchange_rate")
        return {
            "claim_notes": instance.notes,
            "tickets": [self._ticket_row(ticket) for ticket in tickets],
            "total": instance.total_claimed.amount if instance.total_claimed else None,
            "total_currency": currency_symbol(instance.total_claimed_currency),
        }

    @staticmethod
    def _ticket_row(ticket):
        original = ticket.original_amount
        converted = ticket.converted_amount
        return {
            "ticket": ticket,
            "url": reverse("admin:expenses_expenseticket_change", args=[ticket.pk]),
            "vendor": ticket.provider.name if ticket.provider_id else ticket.vendor_name,
            "original": original.amount if original is not None else None,
            "original_currency": currency_symbol(ticket.original_amount_currency),
            # Only worth showing when the ticket was in another currency - for
            # euro receipts it just repeats the original amount.
            "converted": converted.amount if converted is not None else None,
            "converted_currency": currency_symbol(ticket.converted_amount_currency),
            # What stops the claim from being approved as it stands.
            "needs_review": ticket.needs_manual_review or original is None,
        }
