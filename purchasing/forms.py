from django import forms
from django.utils.translation import gettext_lazy as _
from inventory.models import Warehouse
from .models import PurchaseOrderReceipt, PurchaseOrderReceiptItem

from unfold.widgets import UnfoldAdminSelect2Widget

class DirectReceiptForm(forms.ModelForm):
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.all(),
        label=_("Receiving Warehouse"),
        required=True,
        widget=UnfoldAdminSelect2Widget
    )

    class Meta:
        model = PurchaseOrderReceipt
        fields = ["warehouse"]

class DirectReceiptItemForm(forms.Form):
    po_item_id = forms.IntegerField(widget=forms.HiddenInput())
    quantity_to_receive = forms.DecimalField(
        max_digits=12,
        decimal_places=4,
        min_value=0,
        required=False,
        label=_("Quantity to Receive")
    )
