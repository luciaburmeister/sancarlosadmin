from django import forms
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.timezone import localtime
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.company_access import get_user_companies

from unfold.admin import ModelAdmin, TabularInline, GenericTabularInline
from unfold.decorators import action
from unfold.enums import ActionVariant
from unfold.widgets import UnfoldAdminSelect2Widget
from unfold.contrib.filters.admin import AutocompleteSelectFilter
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import ExportForm, ImportForm, SelectableFieldsExportForm
from inventory.resources import InventoryItemResource

from django.urls import path, reverse
from django.shortcuts import redirect

from .views import AdjustStockView, ItemIdentityCodesView, ReceiveStockView, TransferStockView
from .qr_views import item_qr_view
from .models import (
    Warehouse,
    StockLocation,
    InventoryCategory,
    InventoryItem,
    ItemProviderInfo,
    ItemTransaction,
    ItemIdentityCode,
    BillOfMaterialsLine,
)
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
class WarehouseAdmin(ModelAdmin):
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
        exclude = ("product_object",)
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
        return cleaned_data


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
        if self.value() == "low":
            return qs.filter(min_stock__isnull=False, _stock__lte=F("min_stock"))
        if self.value() == "ok":
            return qs.exclude(min_stock__isnull=False, _stock__lte=F("min_stock"))
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
        "min_stock_display",
        "reorder_action_display",
    )
    list_filter = (
        "item_type",
        LowStockFilter,
        ["default_warehouse", AutocompleteSelectFilter],
        ["preferred_supplier", AutocompleteSelectFilter],
    )
    search_fields = (
        "code",
        "name",
        "preferred_supplier__name",
        "default_warehouse__code",
        "default_warehouse__name",
        "provider_info__provider_ref",
    )
    ordering = ("-created_at",)
    autocomplete_fields = ("default_warehouse", "preferred_supplier", "product_content_type", "invoice", "company")
    change_form_outer_before_template = "admin/inventory/item_stock_actions.html"
    readonly_fields = (
        "code",
        "warehouse_country",
        "warehouse_city",
        "warehouse_postal_code",
        "warehouse_address",
        "stock_on_hand_display",
    )

    fieldsets = (
        (_("Item"), {
            "fields": (
                ("company"),
                ("code", "item_type"),
                ("name", "stock_on_hand_display"),
                ("selling_price", "cost_price"),
                ("min_stock",),
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
    )

    # ── Display helpers ───────────────────────────────────────────────────

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

        # Amber — above minimum but within 50% of it (getting low)
        if obj.min_stock and obj.min_stock > 0 and qty <= obj.min_stock * 1.5:
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

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
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