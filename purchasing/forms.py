from purchasing.models import PurchaseOrderReceiptItem, ProviderQuoteStatus
from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from inventory.models import Warehouse
from materials.models import Provider
from .models import PurchaseOrderReceipt
from core.admin.company_access import get_user_companies

from unfold.fields import UnfoldAdminMultipleAutocompleteModelChoiceField
from unfold.widgets import (
    UnfoldAdminSelect2Widget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminTextInputWidget,
)

class DirectReceiptForm(forms.ModelForm):
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.none(),
        label=_("Receiving Warehouse"),
        required=True,
        widget=UnfoldAdminSelect2Widget
    )

    class Meta:
        model = PurchaseOrderReceipt
        fields = ["warehouse", "external_reference", "file"]
        widgets = {
            "external_reference": UnfoldAdminTextInputWidget(attrs={
                "placeholder": _("e.g. ALB-2024-001"),
            }),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if user and not user.is_superuser:
            companies = get_user_companies(user)
            self.fields["warehouse"].queryset = Warehouse.objects.filter(
                company__in=companies, is_active=True
            )
        else:
            self.fields["warehouse"].queryset = Warehouse.objects.filter(
                is_active=True
            )

class DirectReceiptItemForm(forms.Form):
    po_item_id = forms.IntegerField(widget=forms.HiddenInput())
    quantity_to_receive = forms.DecimalField(
        max_digits=12,
        decimal_places=4,
        min_value=0,
        required=False,
        label=_("Quantity to Receive")
    )


class IdentityCodesForm(forms.Form):
    """
    Dynamically builds one textarea per receipt item for a PO.
    After clean(), self.parsed_codes = {receipt_item_id: [code, ...]}.
    """

    def __init__(self, *args, purchase_order=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.receipt_items = list(
            PurchaseOrderReceiptItem.objects
            .filter(receipt__purchase_order=purchase_order)
            .select_related("po_item__inventory_item", "receipt")
            .prefetch_related("identity_codes")
            .order_by("receipt__received_at", "pk")
        ) if purchase_order else []

        for ri in self.receipt_items:
            self.fields[f"codes_{ri.id}"] = forms.CharField(
                required=False,
                widget=UnfoldAdminTextareaWidget(attrs={
                    "rows": 3,
                    "placeholder": "SN-001\nSN-002\nSN-003",
                }),
                label=_("New codes"),
            )

    def clean(self):
        cleaned = super().clean()
        self.parsed_codes = {}
        for ri in self.receipt_items:
            raw = cleaned.get(f"codes_{ri.id}", "")
            codes = [c.strip() for c in raw.splitlines() if c.strip()]
            if codes:
                self.parsed_codes[ri.id] = codes
        return cleaned


# ---------------------------------------------------------------------------
# Quote Requests (RFQ)
# ---------------------------------------------------------------------------

class CreateQuotesForm(forms.Form):
    """
    Used from the QuoteRequest detail page to pick which providers should
    receive a ProviderQuote. Uses the same autocomplete-multiple widget
    pattern as the commission agent bulk wizard, resolving choices via an
    AJAX endpoint instead of preloading every provider into the page.
    Providers that already have a quote for this request are simply
    skipped (get_or_create) in the view, so there's no need to exclude
    them here — this keeps the autocomplete endpoint simple and generic.
    """
    providers = UnfoldAdminMultipleAutocompleteModelChoiceField(
        queryset=Provider.objects.all(),
        label=_("Providers"),
        url_path="admin:purchasing_provider_autocomplete",
        required=True,
    )


class QuotePublicResponseForm(forms.Form):
    """
    Dynamically builds one price field per ProviderQuoteItem, plus the
    static fields (shipping, discount, validity, delivery time, notes)
    for the public, non-authenticated provider-facing response page.
    """
    shipping_cost = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
        label=_("Gastos de Envío (€)"),
        help_text=_("Dejalo en blanco si el envío está incluido en el precio."),
    )
    discount_amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
        label=_("Descuento (€)"),
        help_text=_("Importe total a descontar del presupuesto, si aplica."),
    )
    valid_until = forms.DateField(
        required=False,
        label=_("Válido Hasta"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("Fecha límite de validez de este presupuesto."),
    )
    delivery_time_days = forms.IntegerField(
        min_value=0,
        required=False,
        label=_("Plazo de Entrega (días)"),
    )
    provider_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label=_("Notas"),
        help_text=_("Cualquier condición o aclaración adicional sobre este presupuesto."),
    )

    def __init__(self, *args, provider_quote=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.provider_quote = provider_quote
        self.quote_items = list(
            provider_quote.items.select_related("quote_request_item__inventory_item")
        ) if provider_quote else []

        for line in self.quote_items:
            field_name = f"price_{line.id}"
            line.price_field_name = field_name
            self.fields[field_name] = forms.DecimalField(
                max_digits=12,
                decimal_places=4,
                min_value=0,
                required=True,
                initial=line.unit_price.amount if line.unit_price else None,
                label=_("Precio Unitario"),
            )

        if provider_quote:
            self.fields["shipping_cost"].initial = provider_quote.shipping_cost.amount if provider_quote.shipping_cost else None
            self.fields["discount_amount"].initial = provider_quote.discount_amount.amount if provider_quote.discount_amount else None
            self.fields["valid_until"].initial = provider_quote.valid_until
            self.fields["delivery_time_days"].initial = provider_quote.delivery_time_days
            self.fields["provider_notes"].initial = provider_quote.provider_notes

    def save(self):
        provider_quote = self.provider_quote
        for line in self.quote_items:
            line.unit_price = self.cleaned_data.get(f"price_{line.id}")
            line.save()

        provider_quote.shipping_cost = self.cleaned_data.get("shipping_cost") or 0
        provider_quote.discount_amount = self.cleaned_data.get("discount_amount") or 0
        provider_quote.valid_until = self.cleaned_data.get("valid_until")
        provider_quote.delivery_time_days = self.cleaned_data.get("delivery_time_days")
        provider_quote.provider_notes = self.cleaned_data.get("provider_notes", "")
        provider_quote.status = ProviderQuoteStatus.RESPONDED
        provider_quote.responded_at = timezone.now()
        provider_quote.save(update_fields=[
            "shipping_cost", "discount_amount", "valid_until",
            "delivery_time_days", "provider_notes", "status", "responded_at",
        ])
        provider_quote.recalculate_total()
        return provider_quote
