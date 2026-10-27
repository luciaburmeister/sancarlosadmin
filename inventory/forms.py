import datetime

from django import forms
from django.utils.translation import gettext_lazy as _

from unfold.widgets import (
    UnfoldBooleanSwitchWidget,
    UnfoldAdminSelect2Widget,
    UnfoldAdminDateWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminTextInputWidget,
)

from core.forms.dynamic_fields import DynamicFieldBuilder
from materials.models import Provider
from invoices.models import Invoice
from purchasing.models import PurchaseOrderReceiptItem
from .models import ItemIdentityCode, Warehouse
from .services.bulk_stock import build_plan


# ─────────────────────────────────────────────────────────────────────────────
# Item metadata (fields declared by the item's ItemSchema)
# ─────────────────────────────────────────────────────────────────────────────

METADATA_FIELD_PREFIX = "meta__"

_metadata_field_builder = DynamicFieldBuilder(text_max_length=500, boolean_widget=UnfoldBooleanSwitchWidget)


def build_item_metadata_form_fields(schema):
    """One form field per effective field of ``schema``, keyed with the prefix.

    Each field carries ``metadata_source`` (the schema or block defining it)
    so the admin can group them.
    """
    fields = {}
    for spec in schema.effective_fields():
        field = _metadata_field_builder.build(
            field_type=spec.type,
            label=spec.display_label,
            required=spec.required,
            options=spec.options,
            min_value=spec.min_value,
            max_value=spec.max_value,
            help_text=spec.help_text,
        )
        field.metadata_source = spec.source
        fields[f"{METADATA_FIELD_PREFIX}{spec.key}"] = field
    return fields


def metadata_from_cleaned_data(cleaned_data):
    """({key: value} to set, {keys to drop}) from the prefixed form values.

    Empty values are dropped rather than stored as null/"" so an optional
    number left blank does not fail the schema's type check.
    """
    values, empty = {}, set()
    for name, value in cleaned_data.items():
        if not name.startswith(METADATA_FIELD_PREFIX):
            continue
        key = name[len(METADATA_FIELD_PREFIX):]
        if value in (None, "", []):
            empty.add(key)
            continue
        if isinstance(value, (datetime.date, datetime.datetime)):
            value = value.isoformat()
        values[key] = value
    return values, empty


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
        widget=UnfoldAdminSelect2Widget(),
        empty_label=_(" Select warehouse "),
    )
    destination_warehouse = forms.ModelChoiceField(
        label=_("Destination Warehouse"),
        queryset=Warehouse.objects.filter(is_active=True),
        widget=UnfoldAdminSelect2Widget(),
        empty_label=_(" Select warehouse "),
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
        widget=UnfoldAdminSelect2Widget(),
        empty_label=_(" Select warehouse "),
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
        widget=UnfoldAdminSelect2Widget(),
        empty_label=_(" Select invoice "),
    )

    # ── 2. Supplier — pre-filled from invoice but always editable ────────────
    supplier = forms.ModelChoiceField(
        label=_("Supplier"),
        queryset=Provider.objects.all(),
        required=False,
        widget=UnfoldAdminSelect2Widget(),
        empty_label=_(" Select supplier "),
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
        widget=UnfoldAdminSelect2Widget(),
        empty_label=_(" Select warehouse "),
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


# ─────────────────────────────────────────────────────────────────────────────
# Identity codes — bulk registration from the InventoryItem side
# ─────────────────────────────────────────────────────────────────────────────

#: parsed_codes key for the textarea that is not tied to any receipt line.
UNLINKED = None


class ItemIdentityCodesForm(forms.Form):
    """
    Dynamically builds one textarea per receipt line through which this
    InventoryItem was received, mirroring purchasing.forms.IdentityCodesForm
    but pivoted on the item instead of the purchase order — plus one always
    present textarea for codes with no purchase order behind them (historic
    imports, stock counted in by hand).
    After clean(), self.parsed_codes = {receipt_item_id | UNLINKED: [code, ...]}.

    Unlike the PO-side form, every textarea here belongs to the *same* item,
    so a code repeated across two of them would collide on the
    uniq_identity_code_per_item constraint. clean() rejects that outright
    instead of letting get_or_create silently bind it to whichever textarea
    was processed first.
    """

    #: Name of the field holding codes with no receipt line.
    UNLINKED_FIELD = "codes_unlinked"

    codes_unlinked = forms.CharField(
        required=False,
        widget=UnfoldAdminTextareaWidget(attrs={
            "rows": 3,
            "placeholder": "SN-001\nSN-002\nSN-003",
        }),
        label=_("New codes"),
    )

    def __init__(self, *args, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.item = item
        self.receipt_items = list(
            PurchaseOrderReceiptItem.objects
            .filter(po_item__inventory_item=item)
            .select_related("receipt__purchase_order", "po_item")
            .prefetch_related("identity_codes")
            .order_by("-receipt__received_at", "pk")
        ) if item else []

        self.existing_codes = set(
            ItemIdentityCode.objects
            .filter(item=item)
            .values_list("code", flat=True)
        ) if item else set()

        for ri in self.receipt_items:
            self.fields[f"codes_{ri.id}"] = forms.CharField(
                required=False,
                widget=UnfoldAdminTextareaWidget(attrs={
                    "rows": 3,
                    "placeholder": "SN-001\nSN-002\nSN-003",
                }),
                label=_("New codes"),
            )

    @property
    def unlinked_field(self):
        """The bound field for the no-purchase-order textarea."""
        return self[self.UNLINKED_FIELD]

    def _buckets(self):
        """(key, field_name) pairs in the order the template renders them —
        the no-purchase-order box first, then each receipt line. Order decides
        which textarea keeps a code entered twice; the other one gets the
        clash error, so following display order keeps that predictable."""
        return (
            [(UNLINKED, self.UNLINKED_FIELD)]
            + [(ri.id, f"codes_{ri.id}") for ri in self.receipt_items]
        )

    def clean(self):
        cleaned = super().clean()
        self.parsed_codes = {}
        self.skipped_codes = []
        # Which textarea each code was first claimed by, to flag the clash.
        seen = {}

        for key, field_name in self._buckets():
            raw = cleaned.get(field_name, "")
            codes, clashes = [], []
            for code in (c.strip() for c in raw.splitlines()):
                if not code:
                    continue
                if code in self.existing_codes:
                    self.skipped_codes.append(code)
                    continue
                if code in seen and seen[code] != key:
                    clashes.append(code)
                    continue
                if code in codes:
                    continue
                seen[code] = key
                codes.append(code)

            if clashes:
                self.add_error(field_name, _(
                    "These codes are already entered elsewhere on this form: "
                    "%(codes)s"
                ) % {"codes": ", ".join(sorted(set(clashes)))})

            if codes:
                self.parsed_codes[key] = codes

        return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Bulk stock update
# ─────────────────────────────────────────────────────────────────────────────

class BulkStockUpdateForm(forms.Form):
    """Pasted ``code ; counted quantity [; warehouse]`` rows.

    Validating the form resolves every row (see
    inventory.services.bulk_stock.build_plan) and exposes the result as
    ``self.plan`` — valid or not, so the page can mark the bad rows.
    """

    rows = forms.CharField(
        label=_("Rows"),
        widget=UnfoldAdminTextareaWidget(attrs={
            "rows": 14,
            "spellcheck": "false",
            "class": "font-mono",
            "placeholder": "STK-00012;40;WH1\nSTK-00013;0",
        }),
    )
    note = forms.CharField(
        label=_("Note"),
        required=False,
        widget=UnfoldAdminTextInputWidget(attrs={"placeholder": _("Bulk stock update")}),
    )

    def __init__(self, *args, companies, **kwargs):
        super().__init__(*args, **kwargs)
        self.companies = companies
        self.plan = None

    def clean(self):
        cleaned_data = super().clean()
        text = cleaned_data.get("rows")
        if not text:
            return cleaned_data
        self.plan = build_plan(text, self.companies)
        for error in self.plan.errors:
            self.add_error("rows", error)
        invalid = sum(not r.is_valid for r in self.plan.rows)
        if invalid:
            self.add_error("rows", _("%(count)d row(s) have errors; fix them below.") % {"count": invalid})
        return cleaned_data
