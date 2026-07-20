from django import forms
from django.utils.translation import gettext_lazy as _

from djmoney.settings import CURRENCY_CHOICES
from unfold.forms import BaseDialogForm
from unfold.widgets import UnfoldAdminDecimalFieldWidget, UnfoldAdminSelectWidget

from apps.expenses.models import ExpenseTicket


class ExpenseTicketExchangeRateOverrideForm(BaseDialogForm):
    """Dialog form for the ticket's 'Override Exchange Rate' action - the fetched rate/currencies stay view-only, this is the only way to change them."""

    from_currency = forms.ChoiceField(
        label=_("From Currency"),
        choices=CURRENCY_CHOICES,
        widget=UnfoldAdminSelectWidget,
    )
    to_currency = forms.ChoiceField(
        label=_("To Currency"),
        choices=CURRENCY_CHOICES,
        widget=UnfoldAdminSelectWidget,
    )
    rate = forms.DecimalField(
        label=_("New Rate"),
        max_digits=12,
        decimal_places=6,
        widget=UnfoldAdminDecimalFieldWidget,
        help_text=_("How many units of the 'To' currency equal 1 unit of the 'From' currency."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.object_id:
            return
        ticket = ExpenseTicket.objects.filter(pk=self.object_id).select_related("exchange_rate").first()
        if not ticket:
            return
        if ticket.exchange_rate_id:
            self.fields["from_currency"].initial = ticket.exchange_rate.from_currency
            self.fields["to_currency"].initial = ticket.exchange_rate.to_currency
            self.fields["rate"].initial = ticket.exchange_rate.rate
        else:
            # No conversion applied yet (e.g. OCR thought no conversion was
            # needed) - default the "from" side to the ticket's own currency
            # so the user mainly has to fix "to" and/or the rate.
            self.fields["from_currency"].initial = ticket.original_amount_currency
