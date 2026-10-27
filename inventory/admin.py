from decimal import Decimal

import json

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.contrib.contenttypes.models import ContentType
from django.utils.formats import date_format
from django.utils.html import format_html, format_html_join
from django.utils.timezone import localtime
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.company_access import get_user_companies

from unfold.admin import ModelAdmin, TabularInline, GenericTabularInline
from unfold.decorators import action
from unfold.enums import ActionVariant
from unfold.widgets import UnfoldAdminSelect2Widget, UnfoldAdminTextareaWidget
from unfold.contrib.filters.admin import AutocompleteSelectFilter
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import ExportForm, ImportForm, SelectableFieldsExportForm
from inventory.resources import InventoryItemResource

from django.urls import path, reverse
from django.shortcuts import redirect

from .views import AdjustStockView, ItemIdentityCodesView, ReceiveStockView, TransferStockView
from .qr_views import item_qr_view
from .views import (
    AdjustStockView,
    IncomingPurchaseOrdersView,
    ItemIdentityCodesView,
    ReceiveStockView,
    ReservedByQuotesView,
    TransferStockView,
)
from .views_reorder_report import InventoryReportsView, ReorderReportExportView
from .views_bulk_stock import BulkStockUpdateView
from .models import (
    Warehouse,
    StockLocation,
    InventoryCategory,
    InventoryItem,
    ItemProviderInfo,
    ItemTransaction,
    ItemIdentityCode,
    BillOfMaterialsLine,
    ItemSchema,
    ItemSchemaField,
)
from .forms import METADATA_FIELD_PREFIX, build_item_metadata_form_fields, metadata_from_cleaned_data
from general.metadata_schema import validate_data
from inventory.choices import MovementType
from inventory.services.stock_position import refresh_stock_position
from invoices.models import AppliedTax
from purchasing.models import PurchaseOrderItem

from inventory.views import InvoiceInfoView

# ─────────────────────────────────────────────────────────────────────────────
# ContentType (utility)
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(ContentType)
class CustomContentTypeAdmin(admin.ModelAdmin):
    search_fields = ("app_label", "model")
    list_display = ("app_label", "model")


# ─────────────────────────────────────────────────────────────────────────────
# Warehouse
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Warehouse)
class WarehouseAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("id", "code", "name", "city", "country", "is_active", "created_at")
    list_filter = ("is_active", "country", "city")
    search_fields = ("code", "name", "city", "postal_code", "address_line1", "address_line2")
    ordering = ("-created_at",)
    list_editable = ("is_active",)


# ─────────────────────────────────────────────────────────────────────────────
# StockLocation
# ─────────────────────────────────────────────────────────────────────────────

class StockLocationInline(TabularInline):
    model = StockLocation
    extra = 0
    fields = ("location_type", "name", "code", "content_type", "object_id", "is_active")
    raw_id_fields = ("content_type",)
    show_change_link = True
    verbose_name = _("Stock Location")
    verbose_name_plural = _("Stock Locations")


@admin.register(StockLocation)
class StockLocationAdmin(ModelAdmin):
    list_display = ("id", "warehouse", "location_type", "name", "code", "is_active", "source_repr")
    list_filter = (
        ["warehouse", AutocompleteSelectFilter],
        "is_active",
        "location_type",
    )
    search_fields = ("name", "code", "warehouse__name", "warehouse__code", "content_type__app_label")
    readonly_fields = ("source_repr",)
    raw_id_fields = ("content_type",)

    fieldsets = (
        (None, {
            "fields": ("warehouse", "location_type", "name", "code", "is_active"),
        }),
        (_("Linked source (required)"), {
            "fields": ("content_type", "object_id", "source_repr"),
            "description": _(
                "Link this location to any object (material, warehouse master, etc.). "
                "This location must be linked to a source object."
            ),
        }),
    )

    def source_repr(self, obj):
        return str(obj.source_object) if obj and obj.source_object else "-"
    source_repr.short_description = _("Linked source")


# ─────────────────────────────────────────────────────────────────────────────
# InventoryCategory
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(InventoryCategory)
class InventoryCategoryAdmin(ModelAdmin):
    list_display = ("id", "name", "created_at")
    search_fields = ("name",)
    ordering = ("name",)


# ─────────────────────────────────────────────────────────────────────────────
# ItemTransaction inline — read-only history inside InventoryItem
# ─────────────────────────────────────────────────────────────────────────────

class ItemTransactionInline(TabularInline):
    model = ItemTransaction
    extra = 0
    ordering = ("-created_at",)
    tab = True
    verbose_name = _("Transaction")
    verbose_name_plural = _("Transaction History")
    show_change_link = True

    fields = (
        "created_at_display",
        "movement_type",
        "quantity_change_display",
        "warehouse",
        "location",
        "user",
        "note",
    )
    readonly_fields = (
        "created_at_display",
        "quantity_change_display",
        "movement_type",
        "warehouse",
        "location",
        "user",
        "note",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def created_at_display(self, obj):
        if obj and obj.pk:
            return obj.created_at.strftime("%Y-%m-%d %H:%M")
        return "—"
    created_at_display.short_description = _("Date")

    def quantity_change_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        colour = "green" if obj.quantity_change > 0 else "red"
        sign = "+" if obj.quantity_change > 0 else ""
        return format_html(
            '<span style="color:{};font-weight:600;">{}{}</span>',
            colour, sign, obj.quantity_change,
        )
    quantity_change_display.short_description = _("Quantity")


class ItemProviderInfoInline(TabularInline):
    model = ItemProviderInfo
    extra = 0
    autocomplete_fields = ("provider","item")
    verbose_name = _("Provider Reference")
    verbose_name_plural = _("Provider References")
    tab = True


def _muted_dash():
    return format_html('<span style="color:#9ca3af;">—</span>')


def _stacked(main_html, sub_text):
    """A bold primary line with a small muted line underneath."""
    if not sub_text:
        return main_html
    return format_html(
        '{}<div style="font-size:11px;color:#9ca3af;">{}</div>', main_html, sub_text
    )


def _short_date(value):
    return date_format(localtime(value), "SHORT_DATE_FORMAT") if value else ""


class _BaseItemIdentityCodeInline(TabularInline):
    """Shared read-only plumbing for the two identity-code inlines.

    Both point at ItemIdentityCode through the same FK, so they would collide
    on the default formset prefix ("identity_codes") and share one management
    form. prefix_suffix keeps them apart.
    """

    model = ItemIdentityCode
    extra = 0
    max_num = 0
    can_delete = False
    tab = True
    prefix_suffix = None

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        suffix = self.prefix_suffix
        base_prefix = formset.get_default_prefix()

        class PrefixedFormSet(formset):
            @classmethod
            def get_default_prefix(cls):
                return f"{base_prefix}-{suffix}"

        return PrefixedFormSet

    def code_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        return format_html(
            '<a href="{}" style="font-family:ui-monospace,monospace;font-weight:600;">{}</a>',
            reverse("admin:inventory_itemidentitycode_change", args=[obj.pk]),
            obj.code,
        )
    code_display.short_description = _("Code")

    def source_display(self, obj):
        if not obj or not obj.pk or not obj.receipt_item_id:
            return _muted_dash()
        po = obj.receipt_item.receipt.purchase_order
        return _stacked(
            format_html(
                '<a href="{}" style="font-weight:600;">{}</a>',
                reverse("admin:purchasing_purchaseorder_change", args=[po.pk]),
                po.number or po.pk,
            ),
            _short_date(obj.received_at),
        )
    source_display.short_description = _("Received in")


class ItemIdentityCodeInline(_BaseItemIdentityCodeInline):
    """Units still held: received and not yet shipped, plus any registered by
    hand without a receipt behind them. Delivered units move to
    DeliveredItemIdentityCodeInline."""

    verbose_name = _("Identity Code")
    verbose_name_plural = _("Identity Codes — In Stock")
    prefix_suffix = "stock"
    template_before = "admin/inventory/identity_codes_actions.html"
    fields = ("code_display", "status_display", "source_display", "note")
    readonly_fields = fields

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .filter(delivery_order_item__isnull=True)
            .select_related("receipt_item__receipt__purchase_order")
            .order_by("code")
        )

    def status_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        label, color = (
            (_("In stock"), "#16a34a") if obj.is_received
            else (_("Registered"), "#9ca3af")
        )
        return format_html(
            '<span style="display:inline-flex;align-items:center;gap:5px;'
            'white-space:nowrap;font-weight:600;color:{};">'
            '<span style="width:7px;height:7px;border-radius:50%;background:{};"></span>{}'
            '</span>',
            color, color, label,
        )
    status_display.short_description = _("Status")


class DeliveredItemIdentityCodeInline(_BaseItemIdentityCodeInline):
    """Units already shipped, with the delivery-order side of the trail:
    which order, which client, which line, and when."""

    verbose_name = _("Delivered Identity Code")
    verbose_name_plural = _("Identity Codes — Delivered")
    prefix_suffix = "delivered"
    fields = ("code_display", "delivery_order_display", "client_display",
              "delivery_line_display", "source_display", "note")
    readonly_fields = fields

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .filter(delivery_order_item__isnull=False)
            .select_related(
                "receipt_item__receipt__purchase_order",
                "delivery_order_item__delivery_order__client",
            )
            .order_by("-delivered_at", "code")
        )

    def delivery_order_display(self, obj):
        if not obj or not obj.pk or not obj.delivery_order_item_id:
            return _muted_dash()
        do = obj.delivery_order_item.delivery_order
        return _stacked(
            format_html(
                '<a href="{}" style="font-weight:600;">{}</a>',
                reverse("admin:sales_deliveryorder_change", args=[do.pk]),
                do.number or do.pk,
            ),
            _short_date(obj.delivered_at),
        )
    delivery_order_display.short_description = _("Delivery Order")

    def client_display(self, obj):
        if not obj or not obj.pk or not obj.delivery_order_item_id:
            return _muted_dash()
        client = obj.delivery_order_item.delivery_order.client
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:sales_client_change", args=[client.pk]),
            client.name,
        )
    client_display.short_description = _("Client")

    def delivery_line_display(self, obj):
        if not obj or not obj.pk or not obj.delivery_order_item_id:
            return _muted_dash()
        line = obj.delivery_order_item
        return _stacked(
            format_html('<span style="font-weight:600;">{}</span>',
                        line.item_number or "—"),
            line.description,
        )
    delivery_line_display.short_description = _("Delivery Line")


class BillOfMaterialsInline(TabularInline):
    model = BillOfMaterialsLine
    fk_name = "parent"
    extra = 0
    tab = True
    verbose_name = _("BOM Component")
    verbose_name_plural = _("Bill of Materials")
    autocomplete_fields = ("component",)
    fields = ("component", "quantity")



# ─────────────────────────────────────────────────────────────────────────────
# InventoryItem form
# ─────────────────────────────────────────────────────────────────────────────

class InventoryItemForm(forms.ModelForm):
    product_object_selector = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label=_("Product object"),
        help_text=_("Select a Product content type first, then choose the linked object here."),
        # UnfoldAdminSelect2Widget renders a searchable Select2 dropdown from local
        # <option> elements — no AJAX endpoint needed, matches the Unfold autocomplete style.
        widget=UnfoldAdminSelect2Widget(),
    )

    class Meta:
        model = InventoryItem
        # metadata is edited through the meta__<key> fields InventoryItemAdmin
        # declares from the item's schema (see build_item_metadata_form_fields).
        exclude = ("product_object", "metadata")
        # Same widget for item_type so it gets the searchable Select2 treatment too
        widgets = {
            "item_type": UnfoldAdminSelect2Widget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        content_type = None

        if self.instance and self.instance.pk and self.instance.product_content_type_id:
            content_type = self.instance.product_content_type

        if "product_content_type" in self.data:
            try:
                ct_id = int(self.data["product_content_type"])
                content_type = ContentType.objects.get(pk=ct_id)
            except (ValueError, ContentType.DoesNotExist):
                content_type = None

        if "image" in self.fields:
            self.fields["image"].required = False

        metadata = self.instance.metadata or {}
        for name in self.fields:
            if name.startswith(METADATA_FIELD_PREFIX):
                self.initial.setdefault(name, metadata.get(name[len(METADATA_FIELD_PREFIX):]))

        if content_type:
            model_class = content_type.model_class()
            if model_class is not None:
                self.fields["product_object_selector"].queryset = model_class.objects.all()
                if self.instance and self.instance.product_object_id:
                    try:
                        self.fields["product_object_selector"].initial = model_class.objects.get(
                            pk=self.instance.product_object_id
                        )
                    except model_class.DoesNotExist:
                        pass
            else:
                self.fields["product_object_selector"].queryset = ContentType.objects.none()
        else:
            self.fields["product_object_selector"].queryset = ContentType.objects.none()

    def clean(self):
        cleaned_data = super().clean()
        content_type = cleaned_data.get("product_content_type")
        product_obj = cleaned_data.get("product_object_selector")

        if bool(content_type) ^ bool(product_obj):
            raise forms.ValidationError(
                _("Select both Product content type and Product object, or leave both empty.")
            )

        resolved_id = product_obj.pk if product_obj else None
        cleaned_data["product_object_id"] = resolved_id
        self.instance.product_object_id = resolved_id

        self._clean_metadata(cleaned_data)
        return cleaned_data

    def _clean_metadata(self, cleaned_data):
        """Merge the meta__ fields into metadata and validate it here.

        Keys outside the schema are kept untouched. Validating in the form
        puts each error on its own field; InventoryItem.clean() repeats the
        check for non-admin saves, and _update_errors drops that duplicate.
        """
        values, empty = metadata_from_cleaned_data(cleaned_data)
        metadata = {k: v for k, v in (self.instance.metadata or {}).items() if k not in empty}
        metadata.update(values)
        self.instance.metadata = metadata

        schema = cleaned_data.get("metadata_schema")
        if schema is None:
            return
        self._metadata_validated = True
        try:
            validate_data(schema.json_schema(), metadata)
        except ValidationError as exc:
            for key, messages in exc.message_dict.items():
                name = f"{METADATA_FIELD_PREFIX}{key}"
                if name in self.fields:
                    if name not in self.errors:
                        self.add_error(name, messages)
                else:
                    self.add_error("metadata_schema", [f"{key}: {m}" for m in messages])

    def _update_errors(self, errors):
        # "metadata" is not a form field: add_error would raise on it.
        error_dict = getattr(errors, "error_dict", None)
        if error_dict and "metadata" in error_dict:
            messages = error_dict.pop("metadata")
            if not getattr(self, "_metadata_validated", False):
                error_dict.setdefault(NON_FIELD_ERRORS, []).extend(messages)
            if not error_dict:
                return
        super()._update_errors(errors)


# ─────────────────────────────────────────────────────────────────────────────
# Low-stock list filter
# ─────────────────────────────────────────────────────────────────────────────

class LowStockFilter(admin.SimpleListFilter):
    title = _("Stock level")
    parameter_name = "stock_level"

    def lookups(self, request, model_admin):
        return [
            ("low", _("Low stock (at or below minimum)")),
            ("ok",  _("Stock OK")),
        ]

    def queryset(self, request, queryset):
        from django.db.models import Sum, F
        from django.db.models.functions import Coalesce

        qs = queryset.annotate(
            _stock=Coalesce(
                Sum("transactions__quantity_change"), 0
            )
        )
        # A composite holds no stock of its own, so it can never be low —
        # the same rule InventoryItem.is_low_stock applies, kept in step here
        # or the list would contradict the badge on each row.
        if self.value() == "low":
            return qs.filter(
                has_components=False,
                min_stock__isnull=False,
                _stock__lte=F("min_stock"),
            )
        if self.value() == "ok":
            return qs.exclude(
                has_components=False,
                min_stock__isnull=False,
                _stock__lte=F("min_stock"),
            )
        return queryset


class ItemAppliedTaxInline(GenericTabularInline):
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    verbose_name = _("Applied tax")
    verbose_name_plural = _("Applied taxes")
    tab = True

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tax_type")

class PurchaseHistoryInline(TabularInline):
    model = PurchaseOrderItem
    fk_name = "inventory_item"
    extra = 0
    ordering = ("-purchase_order__date_ordered",)
    tab = True
    verbose_name = _("Purchase")
    verbose_name_plural = _("Purchase History")

    fields = (
        "po_number_display",
        "provider_display",
        "date_ordered_display",
        "unit_price",
        "quantity",
        "line_total",
        "received_quantity",
        "last_received_display",
    )
    readonly_fields = (
        "po_number_display",
        "provider_display",
        "date_ordered_display",
        "unit_price",
        "quantity",
        "line_total",
        "received_quantity",
        "last_received_display",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("purchase_order__provider")
            .prefetch_related("receipt_lines__receipt")
        )

    def po_number_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        url = reverse("admin:purchasing_purchaseorder_change", args=[obj.purchase_order_id])
        return format_html('<a href="{}">{}</a>', url, obj.purchase_order.number)
    po_number_display.short_description = _("PO Number")

    def provider_display(self, obj):
        return obj.purchase_order.provider.name if obj and obj.pk else "—"
    provider_display.short_description = _("Provider")

    def date_ordered_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        return obj.purchase_order.date_ordered.strftime("%Y-%m-%d")
    date_ordered_display.short_description = _("Date Ordered")

    def last_received_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        receipts = obj.receipt_lines.all()
        if not receipts:
            return format_html('<span style="color:#9ca3af;">—</span>')
        last = max(r.receipt.received_at for r in receipts)
        return last.strftime("%Y-%m-%d %H:%M")
    last_received_display.short_description = _("Last Received")


@admin.register(InventoryItem)
class InventoryItemAdmin(CompanyRestrictedAdmin, ModelAdmin, ImportExportModelAdmin):
    resource_classes = [InventoryItemResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm
    form = InventoryItemForm
    inlines = [BillOfMaterialsInline, ItemProviderInfoInline, ItemIdentityCodeInline, DeliveredItemIdentityCodeInline, PurchaseHistoryInline, ItemTransactionInline, ItemAppliedTaxInline]
    actions = ["reset_stock_to_zero"]
    actions_list = ["bulk_stock_update_action"]
    actions_row = ["row_receive_stock", "row_transfer_stock", "row_adjust_stock"]
    actions_detail = ["register_identity_codes"]

    list_display = (
        "code",
        "name",
        "item_type_display",
        "selling_price",
        "product_object_repr",
        "product_content_type_display",
        "default_warehouse_display",
        "preferred_supplier_display",
        "stock_on_hand_display",
        "incoming_quantity_display",
        "reserved_quantity_display",
        "min_stock_display",
        "reorder_action_display",
    )
    list_filter = (
        "item_type",
        "has_components",
        "exclude_from_commission",
        LowStockFilter,
        "metadata_schema",
        ["default_warehouse", AutocompleteSelectFilter],
        ["preferred_supplier", AutocompleteSelectFilter],
    )
    list_filter_submit = True
    search_fields = (
        "code",
        "barcode",
        "name",
        "preferred_supplier__name",
        "default_warehouse__code",
        "default_warehouse__name",
        "provider_info__provider_ref",
    )
    ordering = ("-created_at",)
    autocomplete_fields = ("default_warehouse", "preferred_supplier", "product_content_type", "invoice", "company", "business_unit", "metadata_schema")
    change_form_outer_before_template = "admin/inventory/item_stock_actions.html"
    readonly_fields = (
        "code",
        "warehouse_country",
        "warehouse_city",
        "warehouse_postal_code",
        "warehouse_address",
        "stock_on_hand_display",
        "incoming_quantity_display",
        "reserved_quantity_display",
        "metadata_json_display",
    )

    fieldsets = (
        (_("Item"), {
            "fields": (
                ("company"),
                ("code", "item_type"),
                "barcode",
                ("name", "business_unit"),
                "stock_on_hand_display",
                ("incoming_quantity_display", "reserved_quantity_display"),
                ("selling_price", "cost_price"),
                ("min_stock", "exclude_from_commission"),
                ("description", "image"),
                ("product_content_type", "product_object_selector"),
            ),
            "classes": ("tab",),
        }),
        (_("Warehouse"), {
            "fields": (
                "default_warehouse",
                ("warehouse_country", "warehouse_city"),
                ("warehouse_postal_code", "warehouse_address"),
            ),
            "classes": ("tab",),
        }),
        (_("Supplier"), {
            "fields": (
                "preferred_supplier",
            ),
            "classes": ("tab",),
        }),
        (_("Metadata"), {
            "fields": (
                "metadata_schema",
                "metadata_json_display",
            ),
            "classes": ("tab",),
        }),
    )

    # ── Metadata (fields declared by the item's schema) ───────────────────

    def _metadata_schema_for(self, request, obj):
        """The schema whose fields the form shows.

        On a POST that picks another schema, its fields are built straight
        away so a required one comes back as a field error the user can fill
        in, instead of a validation error on fields they cannot see.
        """
        if request.method == "POST" and "metadata_schema" in request.POST:
            pk = request.POST.get("metadata_schema")
            if not pk:
                return None
            return (
                ItemSchema.objects.filter(company__in=get_user_companies(request.user), pk=pk)
                .prefetch_related("fields")
                .first()
            )
        return obj.metadata_schema if obj else None

    def get_form(self, request, obj=None, **kwargs):
        schema = self._metadata_schema_for(request, obj)
        if schema is not None:
            dynamic_fields = build_item_metadata_form_fields(schema)
            kwargs["form"] = type(f"Dynamic{self.form.__name__}", (self.form,), dynamic_fields)
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        fieldsets = list(super().get_fieldsets(request, obj))
        schema = self._metadata_schema_for(request, obj)
        if schema is None:
            return fieldsets
        meta_fields = [f"{METADATA_FIELD_PREFIX}{spec.key}" for spec in schema.effective_fields()]
        for index, (name, options) in enumerate(fieldsets):
            if name == _("Metadata"):
                fields = ("metadata_schema", *meta_fields, "metadata_json_display")
                fieldsets[index] = (name, {**options, "fields": fields})
        return fieldsets

    @admin.display(description=_("Stored metadata"))
    def metadata_json_display(self, obj):
        if not obj or not obj.metadata:
            return _muted_dash()
        return format_html(
            '<pre style="white-space:pre-wrap;margin:0;">{}</pre>',
            json.dumps(obj.metadata, indent=2, ensure_ascii=False, sort_keys=True),
        )

    def get_queryset(self, request):
        # Incoming and reserved are stored columns, so they come along for
        # free; stock on hand is still computed, and without this annotation
        # its column would cost one extra query per row.
        return super().get_queryset(request).with_stock_on_hand()

    # ── Display helpers ───────────────────────────────────────────────────

    @staticmethod
    def _quantity_badge(qty, *, icon, colour, title, url=None):
        """Icon + quantity in one colour, or the muted dash when there is none.

        The quantity is rendered without trailing zeros (3.000 → 3, 2.500 →
        2.5); ``:f`` keeps a normalised whole number out of exponent form.
        When ``url`` is given and there is a nonzero quantity, the badge is
        wrapped as a link to the breakdown behind it.
        """
        if not qty:
            return _muted_dash()
        badge = format_html(
            '<span style="display:inline-flex;align-items:center;gap:4px;'
            'color:{};font-weight:600;" title="{}">'
            '<span class="material-symbols-outlined" style="font-size:17px;">{}</span>'
            '{}</span>',
            colour, title, icon, f"{Decimal(qty).normalize():f}",
        )
        if not url:
            return badge
        return format_html('<a href="{}" style="text-decoration:none;">{}</a>', url, badge)

    def stock_on_hand_display(self, obj):
        qty = int(obj.stock_on_hand)
        sign = "+" if qty > 0 else ""

        # Red — at or below minimum
        if obj.is_low_stock:
            return format_html(
                '<span style="display:inline-flex;align-items:center;gap:4px;">'
                '<span style="color:#dc2626;font-weight:600;">{}{}</span>'
                '<span class="material-symbols-outlined" style="font-size:17px;color:#dc2626;" '
                'title="{}">warning</span>'
                '</span>',
                sign, qty,
                _("Stock is at or below the minimum threshold"),
            )

        # Amber — above minimum but within 50% of it (getting low). A composite
        # has no stock of its own to run low, so it never warns either way.
        if (
            not obj.is_composite
            and obj.min_stock and obj.min_stock > 0
            and qty <= obj.min_stock * 1.5
        ):
            return format_html(
                '<span style="color:#d97706;font-weight:600;" title="{}">{}{}</span>',
                _("Stock is within 50% of the minimum threshold"),
                sign, qty,
            )

        # Green — well above minimum or no minimum set
        return format_html(
            '<span style="color:#16a34a;font-weight:600;">{}{}</span>',
            sign, qty,
        )
    stock_on_hand_display.short_description = _("Stock on Hand")
    stock_on_hand_display.admin_order_field = "stock_on_hand_total"

    def incoming_quantity_display(self, obj):
        return self._quantity_badge(
            obj.incoming_quantity,
            icon="local_shipping",
            colour="#2563eb",
            title=_("Ordered on approved purchase orders, not yet received — click to see the purchase orders"),
            url=reverse("admin:inventory_item_incoming", args=[obj.pk]) if obj.pk else None,
        )
    incoming_quantity_display.short_description = _("Incoming")
    incoming_quantity_display.admin_order_field = "incoming_quantity"

    def reserved_quantity_display(self, obj):
        qty = obj.reserved_quantity
        return self._quantity_badge(
            qty,
            icon="bookmark",
            colour="#d97706" if qty > obj.stock_on_hand else "#7c3aed",
            title=_("Promised on accepted quotes, not yet delivered — click to see the quotes"),
            url=reverse("admin:inventory_item_reserved", args=[obj.pk]) if obj.pk else None,
        )
    reserved_quantity_display.short_description = _("Reserved")
    reserved_quantity_display.admin_order_field = "reserved_quantity"

    def min_stock_display(self, obj):
        if obj.min_stock is None:
            return format_html('<span style="color:#9ca3af;">—</span>')
        return format_html(
            '<span style="font-weight:600;color:#6b7280;">{}</span>',
            obj.min_stock,
        )
    min_stock_display.short_description = _("Min Stock")
    min_stock_display.admin_order_field = "min_stock"

    def reorder_action_display(self, obj):
        if not obj.is_low_stock:
            return format_html('<span style="color:#9ca3af;">—</span>')
        url = reverse("admin:inventory_item_receive", args=[obj.pk])
        return format_html(
            '<a href="{}" title="{}" '
            'style="display:inline-flex;align-items:center;gap:5px;'
            'padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;'
            'background:var(--color-primary-600,#0d9488);color:#fff;text-decoration:none;'
            'border:1px solid var(--color-primary-700,#0f766e);white-space:nowrap;">'
            '{}'
            '</a>',
            url,
            _("Stock is at or below minimum — click to receive stock"),
            _("Reorder Now"),
        )
    reorder_action_display.short_description = _("Reorder")
    reorder_action_display.allow_tags = True

    def product_object_repr(self, obj):
        return str(obj.product_object) if obj.product_object else "—"
    product_object_repr.short_description = _("Product")

    def item_type_display(self, obj):
        return obj.get_item_type_display()
    item_type_display.short_description = _("Item Type")
    item_type_display.admin_order_field = "item_type"

    def product_content_type_display(self, obj):
        if not obj.product_content_type_id:
            return "-"
        return str(obj.product_content_type)
    product_content_type_display.short_description = _("Product Content Type")
    product_content_type_display.admin_order_field = "product_content_type"

    def default_warehouse_display(self, obj):
        return str(obj.default_warehouse) if obj.default_warehouse_id else "—"
    default_warehouse_display.short_description = _("Default Warehouse")
    default_warehouse_display.admin_order_field = "default_warehouse"

    def preferred_supplier_display(self, obj):
        return str(obj.preferred_supplier) if obj.preferred_supplier_id else "—"
    preferred_supplier_display.short_description = _("Preferred Supplier")
    preferred_supplier_display.admin_order_field = "preferred_supplier"

    def warehouse_country(self, obj):
        if not obj.default_warehouse_id:
            return ""
        country = obj.default_warehouse.country
        return country.name if country else ""
    warehouse_country.short_description = _("Country")

    def warehouse_city(self, obj):
        return getattr(obj.default_warehouse, "city", "") if obj.default_warehouse_id else ""
    warehouse_city.short_description = _("City")

    def warehouse_postal_code(self, obj):
        return getattr(obj.default_warehouse, "postal_code", "") if obj.default_warehouse_id else ""
    warehouse_postal_code.short_description = _("Postal Code")

    def warehouse_address(self, obj):
        if not obj.default_warehouse_id:
            return ""
        wh = obj.default_warehouse
        parts = [wh.address_line1, wh.address_line2]
        return " / ".join(p for p in parts if p)
    warehouse_address.short_description = _("Address")

    # ── QR code list-column ───────────────────────────────────────────────

    def qr_code_link(self, obj):
        if not obj.pk:
            return "—"

        icons = []
        actions = [
            ("receive",  _("Receive QR")),
            ("transfer", _("Transfer QR")),
            ("adjust",   _("Adjust QR")),
        ]
        for movement, label in actions:
            url = reverse(f"admin:inventory_item_qr_{movement}", args=[obj.pk])
            icons.append(format_html(
                '<a href="{}" target="_blank" title="{}" '
                'style="display:inline-flex;align-items:center;color:var(--color-primary-600,#0d9488);">'
                '<span class="material-symbols-outlined" style="font-size:18px;">'
                'qr_code_2</span></a>',
                url, label,
            ))
        return format_html('<span style="display:inline-flex;gap:4px;">{}</span>',
                           format_html("".join(str(i) for i in icons)))
    qr_code_link.short_description = _("QR")
    qr_code_link.allow_tags = True

    # ── Row actions ───────────────────────────────────────────────────────

    @action(
        description=_("Receive"),
        url_path="receive",
        icon="download",
        variant=ActionVariant.SUCCESS,
        permissions=["row_receive_stock"],
    )
    def row_receive_stock(self, request, object_id: int):
        return redirect(reverse("admin:inventory_item_receive", args=[object_id]))

    def has_row_receive_stock_permission(self, request):
        return request.user.has_perm("inventory.change_inventoryitem")

    @action(
        description=_("Transfer"),
        url_path="transfer",
        icon="swap_horiz",
        variant=ActionVariant.INFO,
        permissions=["row_transfer_stock"],
    )
    def row_transfer_stock(self, request, object_id: int):
        return redirect(reverse("admin:inventory_item_transfer", args=[object_id]))

    def has_row_transfer_stock_permission(self, request):
        return request.user.has_perm("inventory.change_inventoryitem")

    @action(
        description=_("Adjust"),
        url_path="adjust",
        icon="balance",
        variant=ActionVariant.WARNING,
        permissions=["row_adjust_stock"],
    )
    def row_adjust_stock(self, request, object_id: int):
        return redirect(reverse("admin:inventory_item_adjust", args=[object_id]))

    def has_row_adjust_stock_permission(self, request):
        return request.user.has_perm("inventory.change_inventoryitem")

    @action(
        description=_("Reset stock to 0"),
        icon="restart_alt",
        variant=ActionVariant.DANGER,
        permissions=["reset_stock"],
    )
    def reset_stock_to_zero(self, request, queryset):
        """Zero every warehouse balance with compensating ADJUST rows (ledger stays append-only)."""
        note = str(_("Stock reset to 0"))
        reset_ids = []
        with transaction.atomic():
            for item in queryset:
                balances = (
                    ItemTransaction.objects.filter(item=item)
                    .values("warehouse")
                    .annotate(total=Sum("quantity_change"))
                    .exclude(total=0)
                )
                rows = [
                    ItemTransaction(
                        item=item,
                        warehouse_id=b["warehouse"],
                        movement_type=MovementType.ADJUST,
                        quantity_change=-b["total"],
                        user=request.user,
                        note=note,
                    )
                    for b in balances
                ]
                if rows:
                    ItemTransaction.objects.bulk_create(rows)
                    reset_ids.append(item.pk)
            refresh_stock_position(reset_ids)
        self.message_user(
            request,
            _("Stock reset to 0 for %(count)d item(s).") % {"count": len(reset_ids)},
            messages.SUCCESS,
        )

    def has_reset_stock_permission(self, request):
        return request.user.has_perm("inventory.can_reset_stock")

    @action(description=_("Bulk stock update"), url_path="bulk-stock-action", icon="inventory")
    def bulk_stock_update_action(self, request):
        return redirect(reverse("admin:inventory_inventoryitem_bulk_stock"))

    def has_bulk_stock_update_action_permission(self, request):
        return request.user.has_perm("inventory.change_inventoryitem")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "reports/",
                self.admin_site.admin_view(
                    InventoryReportsView.as_view(model_admin=self)
                ),
                name="inventory_inventoryitem_reports",
            ),
            path(
                "reorder-report/",
                self.admin_site.admin_view(
                    ReorderReportExportView.as_view(model_admin=self)
                ),
                name="inventory_inventoryitem_reorder_report",
            ),
            path(
                "bulk-stock/",
                self.admin_site.admin_view(
                    BulkStockUpdateView.as_view(model_admin=self)
                ),
                name="inventory_inventoryitem_bulk_stock",
            ),
            path(
                "<int:object_id>/qr/receive/",
                self.admin_site.admin_view(
                    lambda req, object_id: item_qr_view(req, object_id, "receive")
                ),
                name="inventory_item_qr_receive",
            ),
            path(
                "<int:object_id>/qr/transfer/",
                self.admin_site.admin_view(
                    lambda req, object_id: item_qr_view(req, object_id, "transfer")
                ),
                name="inventory_item_qr_transfer",
            ),
            path(
                "<int:object_id>/qr/adjust/",
                self.admin_site.admin_view(
                    lambda req, object_id: item_qr_view(req, object_id, "adjust")
                ),
                name="inventory_item_qr_adjust",
            ),
                        path(
                "<path:object_id>/incoming/",
                self.admin_site.admin_view(
                    IncomingPurchaseOrdersView.as_view(model_admin=self)
                ),
                name="inventory_item_incoming",
            ),
            path(
                "<path:object_id>/reserved/",
                self.admin_site.admin_view(
                    ReservedByQuotesView.as_view(model_admin=self)
                ),
                name="inventory_item_reserved",
            ),
            path(
                "<int:object_id>/invoice-info/",
                self.admin_site.admin_view(InvoiceInfoView.as_view(model_admin=self)),
                name="inventory_item_invoice_info",
            ),
            path(
                "<path:object_id>/receive/",
                self.admin_site.admin_view(
                    ReceiveStockView.as_view(model_admin=self)
                ),
                name="inventory_item_receive",
            ),
            path(
                "<path:object_id>/transfer/",
                self.admin_site.admin_view(
                    TransferStockView.as_view(model_admin=self)
                ),
                name="inventory_item_transfer",
            ),
            path(
                "<path:object_id>/adjust/",
                self.admin_site.admin_view(
                    AdjustStockView.as_view(model_admin=self)
                ),
                name="inventory_item_adjust",
            ),
            path(
                "<int:object_id>/identity-codes/",
                self.admin_site.admin_view(
                    ItemIdentityCodesView.as_view(model_admin=self)
                ),
                name="inventory_item_identity_codes",
            ),
        ]
        return custom_urls + urls

    @action(description=_("Register Identity Codes"), icon="qr_code_2")
    def register_identity_codes(self, request, object_id: int):
        return redirect(reverse("admin:inventory_item_identity_codes", args=[object_id]))


# ─────────────────────────────────────────────────────────────────────────────
# ItemTransaction — audit log only, no add/change/delete
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(ItemTransaction)
class ItemTransactionAdmin(ModelAdmin):
    list_display = (
        "created_at",
        "item",
        "movement_type",
        "quantity_change_display",
        "warehouse",
        "location",
        "reference_repr",
        "user",
    )
    list_filter = (
        "movement_type",
        ["warehouse", AutocompleteSelectFilter],
        ["item", AutocompleteSelectFilter],
    )
    search_fields = (
        "item__code",
        "item__name",
        "warehouse__code",
        "warehouse__name",
        "note",
    )
    ordering = ("-created_at",)
    readonly_fields = (
        "created_at",
        "item",
        "movement_type",
        "quantity_change",
        "warehouse",
        "location",
        "user",
        "note",
        "reference_content_type",
        "reference_object_id",
        "reference_repr",
    )

    fieldsets = (
        (_("Transaction"), {
            "fields": (
                "item",
                ("movement_type", "quantity_change"),
                ("warehouse", "location"),
                "user",
                "note",
            ),
        }),
        (_("Source reference"), {
            "fields": (
                ("reference_content_type", "reference_object_id"),
                "reference_repr",
            ),
            "description": _(
                "Originating document (purchase order, production ticket, etc.)."
            ),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def quantity_change_display(self, obj):
        colour = "green" if obj.quantity_change > 0 else "red"
        sign = "+" if obj.quantity_change > 0 else ""
        return format_html(
            '<span style="color:{};font-weight:600;">{}{}</span>',
            colour, sign, obj.quantity_change,
        )
    quantity_change_display.short_description = _("Quantity")

    def reference_repr(self, obj):
        if obj and obj.reference_object:
            return str(obj.reference_object)
        return "—"
    reference_repr.short_description = _("Reference")


# ─────────────────────────────────────────────────────────────────────────────
# ItemIdentityCode — standalone admin for searching / filtering
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(ItemIdentityCode)
class ItemIdentityCodeAdmin(ModelAdmin):
    list_display = (
        "code",
        "item",
        "received_status",
        "received_at",
        "delivered_status",
        "delivered_at",
    )
    list_filter = (
        ["item", AutocompleteSelectFilter],
    )
    search_fields = (
        "code",
        "item__code",
        "item__name",
    )
    ordering = ("-created_at",)
    autocomplete_fields = ("item", "receipt_item", "delivery_order_item")
    readonly_fields = ("received_at", "delivered_at")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(item__company__in=get_user_companies(request.user))

    fieldsets = (
        (_("Identity Code"), {
            "fields": (
                "item",
                "code",
                "note",
            ),
        }),
        (_("Receiving"), {
            "fields": (
                "receipt_item",
                "received_at",
            ),
        }),
        (_("Delivery"), {
            "fields": (
                "delivery_order_item",
                "delivered_at",
            ),
        }),
    )

    def received_status(self, obj):
        if obj.is_received:
            return format_html(
                '<span style="color:#16a34a;font-weight:600;">✓ {}</span>',
                _("Yes"),
            )
        return format_html('<span style="color:#9ca3af;">—</span>')
    received_status.short_description = _("Received")

    def delivered_status(self, obj):
        if obj.is_delivered:
            return format_html(
                '<span style="color:#2563eb;font-weight:600;">✓ {}</span>',
                _("Yes"),
            )
        return format_html('<span style="color:#9ca3af;">—</span>')
    delivered_status.short_description = _("Delivered")

# ─────────────────────────────────────────────────────────────────────────────
# Item metadata schemas
# ─────────────────────────────────────────────────────────────────────────────

class ItemSchemaFieldFormSet(forms.BaseInlineFormSet):
    """Rejects key collisions with the included blocks before anything is saved.

    ItemSchemaAdmin.save_form stashes the proposed blocks on the instance:
    the M2M is only written after the formsets, so the DB still holds the
    old set at this point.
    """

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        includes = getattr(self.instance, "_proposed_includes", None)
        if includes is None:
            includes = self.instance.includes.all() if self.instance.pk else []
        own_keys = [
            form.cleaned_data["key"].strip().lower().replace(" ", "_").replace("-", "_")
            for form in self.forms
            if form.cleaned_data.get("key") and not form.cleaned_data.get("DELETE")
        ]
        if len(own_keys) != len(set(own_keys)):
            raise ValidationError(_("Each key can only be defined once."))
        self.instance.validate_structure(includes=includes, own_keys=own_keys)


class ItemSchemaFieldInline(TabularInline):
    model = ItemSchemaField
    formset = ItemSchemaFieldFormSet
    extra = 0
    ordering_field = "order"
    hide_ordering_field = True
    fields = ("key", "label", "type", "required", "options", "unit", "min_value", "max_value", "help_text", "order")
    formfield_overrides = {
        models.JSONField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }


@admin.register(ItemSchema)
class ItemSchemaAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "kind", "includes_summary", "fields_summary")
    list_filter = ("kind",)
    search_fields = ("name",)
    autocomplete_fields = ("company", "includes")
    inlines = [ItemSchemaFieldInline]
    readonly_fields = ("effective_fields_display", "json_schema_display")
    fieldsets = (
        (None, {"fields": ("company", ("name", "kind"), "description", "includes")}),
        (_("Effective fields"), {"fields": ("effective_fields_display", "json_schema_display")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("fields", "includes")

    def save_form(self, request, form, change):
        obj = super().save_form(request, form, change)
        if "includes" in form.cleaned_data:
            obj._proposed_includes = list(form.cleaned_data["includes"].prefetch_related("fields"))
        return obj

    @admin.display(description=_("Includes"))
    def includes_summary(self, obj):
        return ", ".join(block.name for block in obj.includes.all()) or _muted_dash()

    @admin.display(description=_("Fields"))
    def fields_summary(self, obj):
        return ", ".join(field.key for field in obj.fields.all()) or _muted_dash()

    @admin.display(description=_("Effective fields"))
    def effective_fields_display(self, obj):
        if not obj or not obj.pk:
            return _muted_dash()
        rows = format_html_join(
            "",
            "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            (
                (
                    field.key,
                    field.display_label,
                    field.get_type_display(),
                    _("Yes") if field.required else "",
                    field.source.name,
                )
                for field in obj.effective_fields()
            ),
        )
        return format_html(
            '<table class="w-full text-left"><thead><tr><th>{}</th><th>{}</th><th>{}</th><th>{}</th><th>{}</th></tr></thead>'
            "<tbody>{}</tbody></table>",
            _("Key"), _("Label"), _("Type"), _("Required"), _("Defined in"), rows,
        )

    @admin.display(description=_("JSON Schema"))
    def json_schema_display(self, obj):
        if not obj or not obj.pk:
            return _muted_dash()
        return format_html(
            '<pre style="white-space:pre-wrap;margin:0;">{}</pre>',
            json.dumps(obj.json_schema(), indent=2, ensure_ascii=False),
        )
