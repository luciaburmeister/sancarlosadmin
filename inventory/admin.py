from django import forms
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin

from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action
from unfold.enums import ActionVariant
from unfold.widgets import UnfoldAdminSelectWidget
from unfold.contrib.filters.admin import AutocompleteSelectFilter

from django.urls import path, reverse
from django.shortcuts import redirect

from .views import AdjustStockView, ReceiveStockView, TransferStockView
from .choices import MovementType
from .models import (
    Warehouse,
    StockLocation,
    InventoryCategory,
    InventoryItem,
    InventoryMovement,
    ItemTransaction,
)

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
    quantity_change_display.short_description = _("Qty Δ")


# ─────────────────────────────────────────────────────────────────────────────
# InventoryItem form
# ─────────────────────────────────────────────────────────────────────────────

class InventoryItemForm(forms.ModelForm):
    product_object_selector = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label=_("Product object"),
        help_text=_("Select a Product content type first, then choose the linked object here."),
        widget=UnfoldAdminSelectWidget,
    )

    class Meta:
        model = InventoryItem
        exclude = ("product_object",)

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
# InventoryItem admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(InventoryItem)
class InventoryItemAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = InventoryItemForm
    inlines = [ItemTransactionInline]
    actions_row = ["row_receive_stock", "row_transfer_stock", "row_adjust_stock"]

    list_display = (
        "code",
        "name",
        "item_type_display",
        "product_object_repr",
        "product_content_type_display",
        "default_warehouse_display",
        "preferred_supplier_display",
        "stock_on_hand_display",
    )
    list_filter = (
        "item_type",
        ["default_warehouse", AutocompleteSelectFilter],
        ["preferred_supplier", AutocompleteSelectFilter],
    )
    search_fields = (
        "code",
        "name",
        "preferred_supplier__name",
        "default_warehouse__code",
        "default_warehouse__name",
    )
    ordering = ("-created_at",)
    autocomplete_fields = ("default_warehouse", "preferred_supplier", "product_content_type", "invoice","company")
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
                "description",
                "image",
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
        colour = "green" if qty > 0 else ("red" if qty < 0 else "gray")
        sign = "+" if qty > 0 else ""
        return format_html(
            '<span style="color:{};font-weight:700;font-size:15px;">{}{}</span>',
            colour, sign, qty,
        )
    stock_on_hand_display.short_description = _("Stock on Hand")

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

    # ── Custom URLs ───────────────────────────────────────────────────────

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
        ]
        return custom_urls + urls

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
    quantity_change_display.short_description = _("Qty Δ")

    def reference_repr(self, obj):
        if obj and obj.reference_object:
            return str(obj.reference_object)
        return "—"
    reference_repr.short_description = _("Reference")