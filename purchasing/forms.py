from purchasing.models import PurchaseOrderReceiptItem
from django import forms
from django.utils.translation import gettext_lazy as _
from inventory.models import Warehouse
from .models import PurchaseOrderReceipt
from core.admin.company_access import get_user_companies

from unfold.widgets import UnfoldAdminSelect2Widget, UnfoldAdminTextareaWidget, UnfoldAdminTextInputWidget

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


class RejectPurchaseOrderForm(forms.Form):
    reason = forms.CharField(
        required=True,
        label=_("Rejection Reason"),
        widget=UnfoldAdminTextareaWidget(attrs={
            "rows": 4,
            "placeholder": _("Explain why this Purchase Order is rejected."),
        }),
    )
