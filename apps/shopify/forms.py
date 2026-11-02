from django import forms


class QuoteRequestForm(forms.Form):
    """What a customer sends from a product page to ask for a quote."""

    variant_id = forms.IntegerField(min_value=1)
    quantity = forms.IntegerField(min_value=1, max_value=100000)
    name = forms.CharField(max_length=200)
    email = forms.EmailField()
    phone = forms.CharField(max_length=50, required=False)
    notes = forms.CharField(max_length=2000, required=False)
    # Hidden from people by the theme: only bots fill it in.
    website = forms.CharField(required=False)
