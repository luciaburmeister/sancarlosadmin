from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from core.admin.company_admin import CompanyRestrictedAdmin
from django.urls import path, reverse
from .models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus,
    PurchaseOrderReceipt, PurchaseOrderReceiptItem
)
from django.shortcuts import redirect
from .views import DirectReceiptView

class PurchaseOrderItemInline(TabularInline):
    model = PurchaseOrderItem
    extra = 1
    tab = True
    autocomplete_fields = ["inventory_item"]
    readonly_fields = ["line_total","formatted_received", "formatted_pending"]
    fields = [
        "inventory_item",
        "quantity",
        "unit",
        "formatted_received",
        "formatted_pending",
        "unit_price",
        "line_total",
        "description",
    ]



    @display(description=_("Received"))
    def formatted_received(self, obj):
        if obj.received_quantity is None:
            return "-"
        return obj.received_quantity

    @display(description=_("Pending"))
    def formatted_pending(self, obj):
        if obj.pk:
            return obj.quantity - obj.received_quantity
        return "-"

    def has_add_permission(self, request, obj=None):
        if obj and obj.status != PurchaseOrderStatus.DRAFT:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj and obj.status != PurchaseOrderStatus.DRAFT:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != PurchaseOrderStatus.DRAFT:
            return False
        return super().has_delete_permission(request, obj)

@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "provider", "requested_by", "get_status_label", "date_ordered", "total_amount", "company"]
    list_filter = ["status", "date_ordered", "company"]
    search_fields = ["number", "provider__name", "requested_by__username", "requested_by__first_name", "requested_by__last_name"]
    autocomplete_fields = ["provider", "company", "approved_by", "requested_by"]
    readonly_fields = ["number", "total_amount", "approved_at", "approved_by", "requested_by", "status"]
    
    fieldsets = (
        (_("General Information"), {
            "fields": (
                "number",
                "company",
                "provider",
                "requested_by",
                "date_ordered",
                "status",
                "file",
            )
        }),
        (_("Financials & Approval"), {
            "fields": (
                "total_amount",
                ("approved_by", "approved_at"),
            )
        }),
        (_("Notes"), {
            "fields": (
                "notes",
            ),
            "classes": ["collapse"],
        }),
    )
    
    inlines = [PurchaseOrderItemInline]
    
    actions_row = ["send_to_approval", "approve_po", "receive_directly"]

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

    def save_model(self, request, obj, form, change):
        if not getattr(obj, "requested_by", None):
            obj.requested_by = request.user
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != PurchaseOrderStatus.DRAFT:
            # If not in draft, everything is readonly
            return [f.name for f in self.model._meta.fields if f.name != "id"] + list(self.readonly_fields)
        return super().get_readonly_fields(request, obj)

    @action(description=_("Send to Approval"))
    def send_to_approval(self, request, object_id: int):
        po = PurchaseOrder.objects.get(pk=object_id)
        if po.status == PurchaseOrderStatus.DRAFT:
            po.status = PurchaseOrderStatus.PENDING_APPROVAL
            po.save()
            self.message_user(request, _("Purchase Order sent for approval."))
        else:
            self.message_user(request, _("Only Draft POs can be sent for approval."), level="error")
        return redirect("admin:purchasing_purchaseorder_changelist")

    @action(description=_("Approve PO"), permissions=["can_approve_purchase_order"])
    def approve_po(self, request, object_id: int):
        po = PurchaseOrder.objects.get(pk=object_id)
        if po.status == PurchaseOrderStatus.PENDING_APPROVAL:
            po.status = PurchaseOrderStatus.APPROVED
            po.approved_by = request.user
            po.approved_at = timezone.now()
            po.save()
            self.message_user(request, _("Purchase Order approved successfully."))
        else:
            self.message_user(request, _("Only Pending POs can be approved."), level="error")
        return redirect("admin:purchasing_purchaseorder_changelist")

    def has_can_approve_purchase_order_permission(self, request, obj=None):
        return request.user.has_perm("purchasing.can_approve_purchase_order")
        
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:po_id>/receive-directly",
                self.admin_site.admin_view(DirectReceiptView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_receive_directly_view",
            ),
        ]
        return custom_urls + urls

    @action(description=_("Receive items directly"),
            url_path="receive_directly-row-action")
    def receive_directly(self, request, object_id: int):
        po = PurchaseOrder.objects.get(pk=object_id)
        if po.status != PurchaseOrderStatus.APPROVED:
            self.message_user(request, _("Only APPROVED Purchase Orders can receive items."), level="error")
            return redirect("admin:purchasing_purchaseorder_change", object_id)
        return redirect(reverse("admin:purchasing_purchaseorder_receive_directly_view", args=[object_id]))

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
