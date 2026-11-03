from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from core.forms.select import StaticSelect2MultipleWidget
from sales.models import Client


class QuoteRequestLineForm(forms.Form):
    """One product of a quote request; ``parameters`` make it a made-to-measure request.

    ``notes`` are the customer's measurements or requirements for that product.
    """

    MAX_PARAMETER_LENGTH = 200
    MAX_NOTES_LENGTH = 500

    variant_id = forms.IntegerField(min_value=1)
    quantity = forms.IntegerField(min_value=1, max_value=100000)
    parameters = forms.JSONField(required=False)
    notes = forms.CharField(max_length=MAX_NOTES_LENGTH, required=False)

    def clean_parameters(self) -> dict[str, str] | None:
        parameters = self.cleaned_data["parameters"]
        if parameters in (None, {}):
            return None
        if not isinstance(parameters, dict) or not all(
            isinstance(name, str) and isinstance(value, (str, int, float)) and len(str(value)) <= self.MAX_PARAMETER_LENGTH
            for name, value in parameters.items()
        ):
            raise ValidationError(_("Invalid made-to-measure values."))
        return {name: str(value) for name, value in parameters.items()}


class QuoteRequestForm(forms.Form):
    """What a customer sends to ask for a quote: one or more products and their contact details.

    ``lines`` is a JSON list of ``QuoteRequestLineForm`` data.
    """

    MAX_LINES = 50

    lines = forms.JSONField()
    name = forms.CharField(max_length=200)
    email = forms.EmailField()
    phone = forms.CharField(max_length=50, required=False)
    notes = forms.CharField(max_length=2000, required=False)
    # Hidden from people by the theme: only bots fill it in.
    website = forms.CharField(required=False)

    def clean_lines(self) -> list[dict]:
        lines = self.cleaned_data["lines"]
        if not isinstance(lines, list) or not 1 <= len(lines) <= self.MAX_LINES:
            raise ValidationError(_("Add between 1 and %(max)s products.") % {"max": self.MAX_LINES})
        cleaned = []
        for line in lines:
            form = QuoteRequestLineForm(line if isinstance(line, dict) else {})
            if not form.is_valid():
                raise ValidationError(_("Invalid product line."))
            cleaned.append(form.cleaned_data)
        return cleaned


class WebStoreAccessForm(forms.Form):
    """The clients whose Shopify customers get web store access: their client's discounts and quotes."""

    clients = forms.ModelMultipleChoiceField(
        queryset=Client.objects.none(),
        widget=StaticSelect2MultipleWidget(),
        label=_("Clients"),
        help_text=_(
            "Each client's Shopify customer is the one with its email, created if needed. They sign in with "
            "that email and see their client's prices and quotes."
        ),
    )

    def __init__(self, *args: Any, company_id: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Only the store's company: a client of another company is rejected, not just hidden.
        self.fields["clients"].queryset = Client.objects.filter(company_id=company_id).order_by("name")
