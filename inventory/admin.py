from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from unfold.admin import ModelAdmin

from inventory.models import (
    Warehouse,  
    StockLocation,
    InventoryCategory,
    InventoryItem,
    InventoryMovement,
    ItemAttachment,
)


# -------------------------
# Warehouse
# -------------------------
@admin.register(Warehouse)
class WarehouseAdmin(ModelAdmin):
    list_display = ("id", "code", "name", "city", "country", "is_active", "created_at")
    list_filter = ("is_active", "country", "city")
    search_fields = ("code", "name", "city", "postal_code", "address_line1", "address_line2")
    ordering = ("-created_at",)

    # If you want quick editing of the active flag:
    list_editable = ("is_active",)


# -------------------------
# Legacy: StockLocation (kept for backwards compatibility)
# -------------------------
class StockLocationInline(admin.TabularInline):
    model = StockLocation
    extra = 0
    fields = ("location_type", "name", "code", "is_active")
    show_change_link = True


@admin.register(StockLocation)
class StockLocationAdmin(ModelAdmin):
    list_display = ("id", "warehouse", "location_type", "name", "code", "is_active", "source_repr")
    list_filter = ("warehouse", "is_active", "location_type")
    search_fields = ("name", "code", "warehouse__name", "warehouse__code", "content_type__app_label")
    readonly_fields = ("source_repr",)
    raw_id_fields = ("content_type",)

    fieldsets = (
        (None, {"fields": ("warehouse", "location_type", "name", "code", "is_active")}),
        (
            "Link to external object (optional)",
            {
                "fields": ("content_type", "object_id", "source_repr"),
                "description": (
                    "Link this location to any object (material, warehouse master, etc.). "
                    "If linked, name/code will be auto-filled when possible."
                ),
            },
        ),
    )

    def source_repr(self, obj):
        return str(obj.source_object) if obj and obj.source_object else "-"
    source_repr.short_description = "Linked source"


# -------------------------
# InventoryCategory (optional; keep if you still use it elsewhere)
# -------------------------
@admin.register(InventoryCategory)
class InventoryCategoryAdmin(ModelAdmin):
    list_display = ("id", "name", "created_at")
    search_fields = ("name",)
    ordering = ("name",)


# -------------------------
# Item Attachments (Inline on Item)
# -------------------------
class ItemAttachmentInline(admin.TabularInline):
    model = ItemAttachment
    extra = 0
    fields = ("label", "file", "created_at")
    readonly_fields = ("created_at",)
    show_change_link = True


# -------------------------
# InventoryItem (ERP Item)
# -------------------------
@admin.register(InventoryItem)
class InventoryItemAdmin(ModelAdmin):
    list_display = (
        "code",
        "name",
        "item_type",
        "default_warehouse",
        "preferred_supplier",
        "stock_on_hand_display",
        "created_at",
    )
    list_display_links = ("code",)
    list_filter = ("item_type", "default_warehouse", "preferred_supplier")
    search_fields = (
        "code",
        "name",
        "preferred_supplier__name",
        "default_warehouse__code",
        "default_warehouse__name",
    )
    ordering = ("-created_at",)

    autocomplete_fields = ("default_warehouse", "preferred_supplier")
    inlines = (ItemAttachmentInline,)

    readonly_fields = (
        "code",
        "warehouse_open_button",
        "warehouse_country",
        "warehouse_city",
        "warehouse_postal_code",
        "warehouse_address",
        "stock_on_hand_display",
    )

    fieldsets = (
        (
            "Item",
            {
                "fields": (
                    "code",
                    "name",
                    "description",
                    "item_type",
                )
            },
        ),
        (
            "Warehouse",
            {
                "fields": (
                    "default_warehouse",
                    "warehouse_open_button",
                    "warehouse_country",
                    "warehouse_city",
                    "warehouse_postal_code",
                    "warehouse_address",
                ),
                "description": "Select a warehouse by code. Address fields are displayed from the warehouse record.",
            },
        ),
        ("Supplier", {"fields": ("preferred_supplier",)}),
        ("Media", {"fields": ("image",)}),
    )

    def stock_on_hand_display(self, obj):
        return obj.stock_on_hand
    stock_on_hand_display.short_description = "Stock on Hand"

    # Read-only "autofill display" fields
    def warehouse_country(self, obj):
        return getattr(obj.default_warehouse, "country", "") if obj.default_warehouse_id else ""
    warehouse_country.short_description = "Country"

    def warehouse_city(self, obj):
        return getattr(obj.default_warehouse, "city", "") if obj.default_warehouse_id else ""
    warehouse_city.short_description = "City"

    def warehouse_postal_code(self, obj):
        return getattr(obj.default_warehouse, "postal_code", "") if obj.default_warehouse_id else ""
    warehouse_postal_code.short_description = "Postal Code"

    def warehouse_address(self, obj):
        if not obj.default_warehouse_id:
            return ""
        wh = obj.default_warehouse
        parts = [wh.address_line1, wh.address_line2]
        return " / ".join([p for p in parts if p])
    warehouse_address.short_description = "Address"

    # "View Warehouse" button
    def warehouse_open_button(self, obj):
        if not obj.default_warehouse_id:
            return "—"
        url = reverse("admin:inventory_warehouse_change", args=[obj.default_warehouse_id])
        return format_html('<a class="button" href="{}" target="_blank">View Warehouse</a>', url)
    warehouse_open_button.short_description = ""


# -------------------------
# InventoryMovement
# -------------------------
@admin.register(InventoryMovement)
class InventoryMovementAdmin(ModelAdmin):
    list_display = ("timestamp", "movement_type", "quantity_change", "item", "warehouse", "user")
    list_filter = ("movement_type", "warehouse")
    search_fields = ("item__code", "item__name", "warehouse__code", "warehouse__name", "note")
    ordering = ("-timestamp",)

    autocomplete_fields = ("item", "warehouse")
