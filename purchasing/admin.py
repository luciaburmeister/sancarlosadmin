from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from core.admin.company_admin import CompanyRestrictedAdmin
from .models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus,
    PurchaseOrderReceipt, PurchaseOrderReceiptItem
)

class PurchaseOrderItemInline(TabularInline):
    model = PurchaseOrderItem
    extra = 1
    tab = True
    autocomplete_fields = ["inventory_item"]
    readonly_fields = ["line_total", "received_quantity"]

@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "provider", "get_status_label", "date_ordered", "total_amount", "company"]
    list_filter = ["status", "date_ordered", "company"]
    search_fields = ["number", "provider__name"]
    autocomplete_fields = ["provider", "company", "approved_by"]
    readonly_fields = ["number", "total_amount", "approved_at", "approved_by"]
    
    inlines = [PurchaseOrderItemInline]
    
    actions_row = ["approve_po"]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            PurchaseOrderStatus.DRAFT: "warning",
            PurchaseOrderStatus.PENDING_APPROVAL: "info",
            PurchaseOrderStatus.APPROVED: "success",
            PurchaseOrderStatus.CLOSED: "neutral",
            PurchaseOrderStatus.CANCELLED: "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status

    @action(description=_("Approve PO"))
    def approve_po(self, request, object_id: int):
        po = PurchaseOrder.objects.get(pk=object_id)
        if po.status in [PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.PENDING_APPROVAL]:
            po.status = PurchaseOrderStatus.APPROVED
            po.approved_by = request.user
            po.approved_at = timezone.now()
            po.save()
            self.message_user(request, _("Purchase Order approved successfully."))
        else:
            self.message_user(request, _("Only Draft or Pending POs can be approved."), level="error")

class PurchaseOrderReceiptItemInline(TabularInline):
    model = PurchaseOrderReceiptItem
    extra = 1
    tab = True
    autocomplete_fields = ["po_item"]

@admin.register(PurchaseOrderReceipt)
class PurchaseOrderReceiptAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "purchase_order", "warehouse", "received_at", "company"]
    list_filter = ["warehouse", "received_at", "company"]
    search_fields = ["number", "purchase_order__number"]
    autocomplete_fields = ["purchase_order", "warehouse", "company", "received_by"]
    readonly_fields = ["number"]
    
    inlines = [PurchaseOrderReceiptItemInline]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        return form

    # Optionally limit po_item choices in the inline to only items from the selected PO
    # This usually requires some form JS or complex queryset filtering.
    # For now we'll keep it simple as the user didn't ask for advanced UI filtering.

@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(ModelAdmin):
    list_display = ["inventory_item", "purchase_order", "quantity", "received_quantity"]
    search_fields = ["inventory_item__name", "purchase_order__number"]
    autocomplete_fields = ["inventory_item", "purchase_order"]

@admin.register(PurchaseOrderReceiptItem)
class PurchaseOrderReceiptItemAdmin(ModelAdmin):
    list_display = ["po_item", "receipt", "quantity_received"]
    autocomplete_fields = ["po_item", "receipt"]
