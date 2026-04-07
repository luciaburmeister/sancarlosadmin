import datetime

from django import forms
from django.utils.translation import gettext_lazy as _

from unfold.widgets import (
    UnfoldAdminSelectWidget,
    UnfoldAdminDateWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminTextInputWidget,
)

from materials.models import Provider
from invoices.models import Invoice
from .models import Warehouse


class _BaseMovementForm(forms.Form):
    note = forms.CharField(
        label=_("Note / Reason"),
        required=False,
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 3}),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Transfer
# ─────────────────────────────────────────────────────────────────────────────

class TransferMovementForm(_BaseMovementForm):
    source_warehouse = forms.ModelChoiceField(
        label=_("Source Warehouse"),
        queryset=Warehouse.objects.filter(is_active=True),
        widget=UnfoldAdminSelectWidget(),
        empty_label=_("— Select warehouse —"),
    )
    destination_warehouse = forms.ModelChoiceField(
        label=_("Destination Warehouse"),
        queryset=Warehouse.objects.filter(is_active=True),
        widget=UnfoldAdminSelectWidget(),
        empty_label=_("— Select warehouse —"),
    )
    quantity = forms.IntegerField(
        label=_("Quantity"),
        min_value=1,
        widget=UnfoldAdminIntegerFieldWidget(),
    )

    def clean(self):
        cleaned_data = super().clean()
        src = cleaned_data.get("source_warehouse")
        dst = cleaned_data.get("destination_warehouse")
        if src and dst and src == dst:
            raise forms.ValidationError(
                _("Source and destination warehouses must be different.")
            )
        return cleaned_data


# ─────────────────────────────────────────────────────────────────────────────
# Adjustment
# ─────────────────────────────────────────────────────────────────────────────

class AdjustmentMovementForm(_BaseMovementForm):
    warehouse = forms.ModelChoiceField(
        label=_("Warehouse"),
        queryset=Warehouse.objects.filter(is_active=True),
        widget=UnfoldAdminSelectWidget(),
        empty_label=_("— Select warehouse —"),
    )
    quantity_change = forms.IntegerField(
        label=_("Quantity Change"),
        widget=UnfoldAdminIntegerFieldWidget(),
    )

    def clean_quantity_change(self):
        val = self.cleaned_data.get("quantity_change")
        if val == 0:
            raise forms.ValidationError(_("Quantity change cannot be zero."))
        return val


# ─────────────────────────────────────────────────────────────────────────────
# Receipt
# ─────────────────────────────────────────────────────────────────────────────

class ReceiptMovementForm(_BaseMovementForm):
    # ── 1. Invoice — FK to Invoice; replaces the old free-text PO reference ──
    invoice = forms.ModelChoiceField(
        label=_("Invoice"),
        queryset=Invoice.objects.select_related("provider").order_by("-date_issued"),
        required=False,
        widget=UnfoldAdminSelectWidget(),
        empty_label=_("— Select invoice —"),
    )

    # ── 2. Supplier — pre-filled from invoice but always editable ────────────
    supplier = forms.ModelChoiceField(
        label=_("Supplier"),
        queryset=Provider.objects.all(),
        required=False,
        widget=UnfoldAdminSelectWidget(),
        empty_label=_("— Select supplier —"),
    )

    # ── 3. Received date ──────────────────────────────────────────────────────
    received_date = forms.DateField(
        label=_("Received Date"),
        widget=UnfoldAdminDateWidget(attrs={"class": "vDateField"}),
    )

    # ── 4. Receiving Warehouse ────────────────────────────────────────────────
    warehouse = forms.ModelChoiceField(
        label=_("Receiving Warehouse"),
        queryset=Warehouse.objects.filter(is_active=True),
        widget=UnfoldAdminSelectWidget(),
        empty_label=_("— Select warehouse —"),
    )

    # ── 5. Quantity ───────────────────────────────────────────────────────────
    quantity = forms.IntegerField(
        label=_("Quantity Received"),
        min_value=1,
        widget=UnfoldAdminIntegerFieldWidget(attrs={"placeholder": "0"}),
    )

    # note is inherited from _BaseMovementForm

    class Media:
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.initial.get("received_date"):
            self.initial["received_date"] = datetime.date.today()