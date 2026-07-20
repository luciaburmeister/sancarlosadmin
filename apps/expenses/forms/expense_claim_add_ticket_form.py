from django import forms
from django.utils.translation import gettext_lazy as _

from apps.expenses.models import ExpenseTicket


class ExpenseClaimAddTicketForm(forms.Form):
    tickets = forms.ModelMultipleChoiceField(
        queryset=ExpenseTicket.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        label=_("Tickets"),
    )

    def __init__(self, *args, claim=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.claim = claim
        self.fields["tickets"].queryset = ExpenseTicket.objects.filter(
            claim__isnull=True,
            company=claim.company,
            submitted_by=claim.submitted_by,
        ).order_by("-date_of_purchase", "-created_at")
