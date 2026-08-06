from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from django_filters.filters import MultipleChoiceFilter
from django.forms import MultipleChoiceField
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
from django.contrib import admin
from django.db import transaction
from django.db.models import Sum
from django.utils.html import format_html_join
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from unfold.admin import ModelAdmin, StackedInline, TabularInline
from unfold.decorators import action, display
from core.admin.company_admin import CompanyRestrictedAdmin
from django.urls import path, reverse
from copy import deepcopy
from .models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus, PurchaseOrderType,
    PurchaseOrderReceipt, PurchaseOrderReceiptItem
)
from inventory.models import ItemIdentityCode
from django.shortcuts import redirect

class PurchaseOrderItemInline(StackedInline):
    model = PurchaseOrderItem
    extra = 1
    tab = True
    autocomplete_fields = ["inventory_item"]
    readonly_fields = ["line_total","formatted_received", "formatted_pending", "formatted_invoiced", "formatted_pending_invoice"]
    fieldsets = (
        (None, {
            "fields": (
                ("inventory_item","unit"),
                ("quantity", "unit_price", ),
                ("line_total","description"),
                ("formatted_received", "formatted_pending", "formatted_invoiced", "formatted_pending_invoice"),
            )
        }),
    )

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

    @display(description=_("Invoiced"))
    def formatted_invoiced(self, obj):
        if obj.pk:
            return obj.invoiced_quantity
        return "-"

    @display(description=_("Pending to Invoice"))
    def formatted_pending_invoice(self, obj):
        if obj.pk:
            return obj.pending_invoice_quantity
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
    list_display = ["number", "provider","external_reference", "po_type", "project_file", "requested_by", "get_status_label", "date_ordered", "total_amount", "get_billing_label", "company"]
    list_filter = [("status",MultipleChoicesDropdownFilter), ("po_type",MultipleChoicesDropdownFilter), ("date_ordered",RangeDateFilter), ("company",AutocompleteSelectMultipleFilter), ("project_file",AutocompleteSelectMultipleFilter)]
    search_fields = ["number", "external_reference","provider__name", "requested_by__username", "requested_by__first_name", "requested_by__last_name", "project_file__reference", "project_file__name"]
    autocomplete_fields = ["provider", "company", "approved_by", "requested_by", "project_file"]
    readonly_fields = ["number", "total_amount", "approved_at", "approved_by", "rejected_at", "rejected_by", "rejection_reason", "requested_by", "status", "linked_invoices_display"]
    list_select_related = ["provider", "company", "requested_by", "project_file"]
    list_filter_submit = True
    fieldsets = (
        (_("General Information"), {
            "fields": (
                ("number",
                "company"),
                ("provider",
                "requested_by"),
                ("date_ordered",
                "status"),
                "po_type",
                "project_file",
                ("external_reference","file")
            )
        }),
        (_("Financials & Approval"), {
            "fields": (
                "total_amount",
                ("approved_by", "approved_at"),
                ("rejected_by", "rejected_at"),
                "rejection_reason",
                "linked_invoices_display",
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

    # On locked (non-draft) POs every field is readonly, so `po_type` is absent
    # from the Alpine form data — the typeof guard keeps project_file visible
    # there; get_fieldsets removes it for locked stock POs instead.
    conditional_fields = {
        "project_file": "typeof po_type === 'undefined' || po_type == 'PROJECT'",
    }

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj and obj.po_type == PurchaseOrderType.STOCK and obj.status != PurchaseOrderStatus.DRAFT:
            fieldsets = deepcopy(fieldsets)
            for _name, opts in fieldsets:
                cleaned = []
                for field in opts["fields"]:
                    if field == "project_file":
                        continue
                    if isinstance(field, (list, tuple)):
                        row = tuple(f for f in field if f != "project_file")
                        if row:
                            cleaned.append(row)
                    else:
                        cleaned.append(field)
                opts["fields"] = cleaned
        return fieldsets

    actions_row = ["send_to_approval", "approve_po", "reject_po", "receive_directly"]
    actions_detail = [
        {
            "title": _("Approval"),
            "icon": "task_alt",
            "items": ["send_to_approval_detail_action", "approve_po_detail_action", "reject_po_detail_action"],
        },
        "register_identity_codes",
        "match_invoice_detail_action",
    ]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            PurchaseOrderStatus.DRAFT: "warning",
            PurchaseOrderStatus.PENDING_APPROVAL: "info",
            PurchaseOrderStatus.APPROVED: "success",
            PurchaseOrderStatus.REJECTED: "danger",
            PurchaseOrderStatus.CLOSED: "neutral",
            PurchaseOrderStatus.CANCELLED: "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status, obj.get_status_display()

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _total_qty=Sum("items__quantity"),
            _total_invoiced=Sum("items__invoiced_quantity"),
        )

    @display(
        description=_("Billing"),
        label={
            "not_invoiced": "warning",
            "partially_invoiced": "info",
            "fully_invoiced": "success",
        },
    )
    def get_billing_label(self, obj):
        total = getattr(obj, "_total_qty", None)
        invoiced = getattr(obj, "_total_invoiced", None)
        if total is None or invoiced is None:
            status = obj.billing_status
        elif not total or invoiced <= 0:
            status = "not_invoiced"
        elif invoiced >= total:
            status = "fully_invoiced"
        else:
            status = "partially_invoiced"

        labels = {
            "not_invoiced": _("Not Invoiced"),
            "partially_invoiced": _("Partially Invoiced"),
            "fully_invoiced": _("Fully Invoiced"),
        }
        return status, labels[status]

    @display(description=_("Linked Invoices"))
    def linked_invoices_display(self, obj):
        if not obj.pk:
            return "-"
        from django.apps import apps
        Invoice = apps.get_model("invoices", "Invoice")
        invoices = Invoice.objects.filter(
            po_allocations__po_item__purchase_order=obj
        ).distinct()
        if not invoices:
            return "-"
        return format_html_join(
            ", ",
            '<a href="{}" class="text-primary-600 underline">{}</a>',
            (
                (
                    reverse("admin:invoices_invoice_change", args=[inv.pk]),
                    inv.invoice_number or f"ID {inv.pk}",
                )
                for inv in invoices
            ),
        )

    def save_model(self, request, obj, form, change):
        if not getattr(obj, "requested_by", None):
            obj.requested_by = request.user
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != PurchaseOrderStatus.DRAFT:
            return [f.name for f in self.model._meta.fields if f.name != "id"] + list(self.readonly_fields)
        return super().get_readonly_fields(request, obj)

    def _send_to_approval(self, request, po):
        from purchasing.tasks import send_purchase_order_pending_approval_notification

        if po.status == PurchaseOrderStatus.DRAFT:
            po.status = PurchaseOrderStatus.PENDING_APPROVAL
            po.save()
            transaction.on_commit(
                lambda: send_purchase_order_pending_approval_notification.delay(po.pk)
            )
            self.message_user(request, _("Purchase Order sent for approval."))
        else:
            self.message_user(request, _("Only Draft POs can be sent for approval."), level="error")

    @action(description=_("Send to Approval"))
    def send_to_approval(self, request, object_id: int):
        po = PurchaseOrder.objects.get(pk=object_id)
        self._send_to_approval(request, po)
        return redirect("admin:purchasing_purchaseorder_changelist")

    @action(description=_("Send to Approval"), permissions=["po_is_draft"])
    def send_to_approval_detail_action(self, request, object_id: int):
        po = PurchaseOrder.objects.get(pk=object_id)
        self._send_to_approval(request, po)
        return redirect(reverse("admin:purchasing_purchaseorder_change", args=[object_id]))

    def has_po_is_draft_permission(self, request, object_id=None):
        if not object_id:
            return True
        return PurchaseOrder.objects.filter(pk=object_id, status=PurchaseOrderStatus.DRAFT).exists()

    def _approve_po(self, request, po):
        from purchasing.tasks import send_purchase_order_decision_notification

        if po.status == PurchaseOrderStatus.PENDING_APPROVAL:
            po.status = PurchaseOrderStatus.APPROVED
            po.approved_by = request.user
            po.approved_at = timezone.now()
            po.save()
            transaction.on_commit(
                lambda: send_purchase_order_decision_notification.delay(po.pk, "approved")
            )
            self.message_user(request, _("Purchase Order approved successfully."))
        else:
            self.message_user(request, _("Only Pending POs can be approved."), level="error")

    @action(description=_("Approve PO"), permissions=["can_approve_purchase_order"])
    def approve_po(self, request, object_id: int):
        po = PurchaseOrder.objects.get(pk=object_id)
        self._approve_po(request, po)
        return redirect("admin:purchasing_purchaseorder_changelist")

    @action(description=_("Approve PO"), permissions=["can_approve_purchase_order", "po_is_pending_approval"])
    def approve_po_detail_action(self, request, object_id: int):
        po = PurchaseOrder.objects.get(pk=object_id)
        self._approve_po(request, po)
        return redirect(reverse("admin:purchasing_purchaseorder_change", args=[object_id]))

    def has_can_approve_purchase_order_permission(self, request, object_id=None):
        return request.user.has_perm("purchasing.can_approve_purchase_order")

    @action(description=_("Reject PO"), permissions=["can_approve_purchase_order"])
    def reject_po(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_reject", args=[object_id]))

    @action(description=_("Reject PO"), permissions=["can_approve_purchase_order", "po_is_pending_approval"])
    def reject_po_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_reject", args=[object_id]))

    def has_po_is_pending_approval_permission(self, request, object_id=None):
        if not object_id:
            return True
        return PurchaseOrder.objects.filter(pk=object_id, status=PurchaseOrderStatus.PENDING_APPROVAL).exists()

    @action(description=_("Register Identity Codes"), icon="qr_code_2")
    def register_identity_codes(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_identity_codes", args=[object_id]))

    @action(description=_("Match Invoice"), icon="link")
    def match_invoice_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_match_invoice", args=[object_id]))

    def get_urls(self):
        from .views import DirectReceiptView, IdentityCodesView, POMatchInvoiceView, RejectPurchaseOrderView
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:po_id>/reject/",
                self.admin_site.admin_view(RejectPurchaseOrderView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_reject",
            ),
            path(
                "<int:po_id>/match-invoice/",
                self.admin_site.admin_view(POMatchInvoiceView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_match_invoice",
            ),
            path(
                "<int:po_id>/receive-directly",
                self.admin_site.admin_view(DirectReceiptView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_receive_directly_view",
            ),
            path(
                "<int:po_id>/identity-codes/",
                self.admin_site.admin_view(IdentityCodesView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_identity_codes",
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
    list_display = ["number", "purchase_order", "external_reference", "warehouse", "received_at", "company"]
    list_filter = ["warehouse", "received_at", "company"]
    search_fields = ["number", "purchase_order__number", "external_reference"]
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
    list_display = ["inventory_item", "purchase_order", "quantity", "received_quantity", "invoiced_quantity"]
    search_fields = ["inventory_item__name", "purchase_order__number"]
    autocomplete_fields = ["inventory_item", "purchase_order"]


class IdentityCodeReceiptInline(TabularInline):
    model = ItemIdentityCode
    fk_name = "receipt_item"
    extra = 1
    verbose_name = _("Identity Code")
    verbose_name_plural = _("Identity Codes")
    fields = ("code", "item", "received_at", "note")
    readonly_fields = ("received_at",)
    autocomplete_fields = ("item",)

@admin.register(PurchaseOrderReceiptItem)
class PurchaseOrderReceiptItemAdmin(ModelAdmin):
    list_display = ["po_item", "receipt", "quantity_received"]
    autocomplete_fields = ["po_item", "receipt"]
    search_fields = ["po_item__inventory_item__name", "receipt__number"]
    inlines = [IdentityCodeReceiptInline]
