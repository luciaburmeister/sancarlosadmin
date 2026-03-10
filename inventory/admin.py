from django import forms
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from unfold.admin import ModelAdmin, TabularInline
from unfold.widgets import UnfoldAdminSelectWidget
from unfold.contrib.filters.admin import AutocompleteSelectFilter

from inventory.models import (
    Warehouse,
    StockLocation,
    InventoryCategory,
    InventoryItem,
    InventoryMovement,
)


# ─────────────────────────────────────────────
# Content Type
# ─────────────────────────────────────────────

@admin.register(ContentType)
class CustomContentTypeAdmin(admin.ModelAdmin):
    search_fields = ("app_label", "model")
    list_display = ("app_label", "model")


# ─────────────────────────────────────────────
# Warehouse
# ─────────────────────────────────────────────

@admin.register(Warehouse)
class WarehouseAdmin(ModelAdmin):
    list_display = ("id", "code", "name", "city", "country", "is_active", "created_at")
    list_filter = ("is_active", "country", "city")
    search_fields = ("code", "name", "city", "postal_code", "address_line1", "address_line2")
    ordering = ("-created_at",)
    list_editable = ("is_active",)


# ─────────────────────────────────────────────
# Stock Location
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Inventory Category
# ─────────────────────────────────────────────

@admin.register(InventoryCategory)
class InventoryCategoryAdmin(ModelAdmin):
    list_display = ("id", "name", "created_at")
    search_fields = ("name",)
    ordering = ("name",)


# ─────────────────────────────────────────────
# Inventory Item
# ─────────────────────────────────────────────

class InventoryItemForm(forms.ModelForm):
    """product_object (GenericFK) is excluded from Meta — Django forbids it in ModelForms.
    product_object_selector is written back to product_object_id in clean() so the
    model's clean() sees a consistent state before save()."""

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

    def save(self, commit=True):
        return super().save(commit=commit)


@admin.register(InventoryItem)
class InventoryItemAdmin(ModelAdmin):
    form = InventoryItemForm

    list_display = (
        "code",
        "name",
        "item_type",
        "product_object_repr",
        "product_content_type",
        "default_warehouse",
        "preferred_supplier",
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
    autocomplete_fields = ("default_warehouse", "preferred_supplier", "product_content_type")
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

    def stock_on_hand_display(self, obj):
        return obj.stock_on_hand
    stock_on_hand_display.short_description = _("Stock on Hand")

    def product_object_repr(self, obj):
        return str(obj.product_object) if obj.product_object else "—"
    product_object_repr.short_description = _("Product")

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
