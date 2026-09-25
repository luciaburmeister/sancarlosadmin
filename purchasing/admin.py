from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from django_filters.filters import MultipleChoiceFilter
from django.forms import MultipleChoiceField
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
from django.contrib import admin
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum, Exists, OuterRef
from django.utils.formats import date_format
from django.utils.html import format_html_join, format_html
from django.utils.translation import gettext_lazy as _
from django.utils import timezone, translation
from django.core.exceptions import ValidationError
from unfold.admin import ModelAdmin, StackedInline, TabularInline
from unfold.decorators import action, display
from unfold.sections import TemplateSection
from core.admin.company_admin import CompanyRestrictedAdmin
from django.urls import path, reverse
from copy import deepcopy
from urllib.parse import urlparse
from import_export.admin import ExportActionModelAdmin
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from core.admin.xlsx_export import FormattedXlsxExportMixin
from .models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseOrderMessage, PurchaseOrderStatus, PurchaseOrderType,
    PurchaseOrderReceipt, PurchaseOrderReceiptItem,
    QuoteRequest, QuoteRequestItem, QuoteRequestStatus,
    ProviderQuote, ProviderQuoteItem, ProviderQuoteStatus,
)
from .resources import PurchaseOrderResource
from inventory.models import ItemIdentityCode
from django.shortcuts import redirect
from .views import DirectReceiptView, IdentityCodesView, POMatchInvoiceView, POAIMatchView, DispatchPOAIMatchView, RejectPurchaseOrderView, POApprovalAnalysisView, PostPurchaseOrderMessageView, PurchaseOrderPdfView, PurchaseOrderPdfDownloadView

class PurchaseOrderItemInline(StackedInline):
    model = PurchaseOrderItem
    extra = 1
    tab = True
    autocomplete_fields = ["inventory_item"]
    readonly_fields = ["line_total","formatted_received", "formatted_pending", "formatted_invoiced", "formatted_pending_invoice", "formatted_invoice_match"]
    fieldsets = (
        (None, {
            "fields": (
                ("inventory_item","unit"),
                ("quantity", "unit_price", ),
                ("line_total","description"),
                ("formatted_received", "formatted_pending", "formatted_invoiced", "formatted_pending_invoice"),
                ("formatted_invoice_match",),
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

    @display(
        description=_("Invoice Match"),
        label={
            "Matched": "success",
            "Partial": "warning",
            "Not matched": "danger",
        },
    )
    def formatted_invoice_match(self, obj):
        if not obj.pk:
            return "-"
        if obj.quantity <= 0:
            return "Not matched"
        if obj.invoiced_quantity >= obj.quantity:
            return "Matched"
        if obj.invoiced_quantity > 0:
            return "Partial"
        return "Not matched"

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

def _user_display_name(user):
    if user is None:
        return None
    return user.get_full_name() or user.username


class PurchaseOrderDetailsSection(TemplateSection):
    """Expandable changelist row holding the secondary PO fields dropped from
    list_display (type, cost center, requester, payment terms, approval
    trail) so the main table stays narrow."""

    template_name = "purchasing/po_details_section.html"

    def get_context_data(self, request, instance):
        project_file = instance.project_file
        cost_center = instance.cost_center
        return {
            "po_type": instance.get_po_type_display(),
            "project_file": project_file,
            "project_file_url": (
                reverse("admin:management_projectfile_change", args=[project_file.pk])
                if project_file else None
            ),
            "cost_center": cost_center,
            "cost_center_url": (
                reverse("admin:accounting_costcenter_change", args=[cost_center.pk])
                if cost_center else None
            ),
            "requested_by_name": _user_display_name(instance.requested_by),
            "created_at": instance.created_at,
            "external_reference": instance.external_reference,
            "payment_method": instance.get_payment_method_display(),
            "payment_terms_days": instance.payment_terms_days,
            "approved_by_name": _user_display_name(instance.approved_by),
            "approved_at": instance.approved_at,
            "rejected_by_name": _user_display_name(instance.rejected_by),
            "rejected_at": instance.rejected_at,
            "rejection_reason": instance.rejection_reason,
        }
class PurchaseOrderMessageInline(TabularInline):
    """
    Produces the Conversation tab, which Unfold builds from the inlines that
    set `tab = True`. The thread is rendered from context and posted through
    PostPurchaseOrderMessageView, so this formset stays empty.
    """
    model = PurchaseOrderMessage
    tab = True
    extra = 0
    max_num = 0
    can_delete = False
    show_count = True
    # The tab label uses the singular whenever max_num == 1.
    verbose_name = _("Conversation")
    verbose_name_plural = _("Conversation")
    template = "purchasing/edit_inline/conversation.html"
    fields = ["body"]

    def get_queryset(self, request):
        return super().get_queryset(request).none()

    def has_view_permission(self, request, obj=None):
        if obj is None:
            return True
        return obj.user_can_view_conversation(request.user)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_count(self, request, obj=None):
        # The add view passes an unsaved instance rather than None.
        return obj.messages.count() if obj and obj.pk else 0


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(FormattedXlsxExportMixin, CompanyRestrictedAdmin, ModelAdmin, ExportActionModelAdmin):
    resource_classes = [PurchaseOrderResource]
    export_form_class = SelectableFieldsExportForm
    list_display = ["number", "provider", "get_status_label", "get_date_ordered", "total_amount_display", "get_projected_margin", "get_billing_label", "company"]
    list_sections = [PurchaseOrderDetailsSection]
    list_filter = [("status",MultipleChoicesDropdownFilter), ("po_type",MultipleChoicesDropdownFilter), ("date_ordered",RangeDateFilter), ("company",AutocompleteSelectMultipleFilter), ("cost_center",AutocompleteSelectMultipleFilter), ("project_file",AutocompleteSelectMultipleFilter)]
    search_fields = ["number", "external_reference","provider__name", "requested_by__username", "requested_by__first_name", "requested_by__last_name", "project_file__reference", "project_file__name", "cost_center__code", "cost_center__name"]
    autocomplete_fields = ["provider", "company", "approved_by", "requested_by", "project_file", "cost_center"]
    readonly_fields = ["number", "total_amount_display", "total_amount_with_tax_display", "approved_at", "approved_by", "rejected_at", "rejected_by", "rejection_reason", "requested_by", "status", "linked_invoices_display"]
    list_select_related = ["provider", "company", "requested_by", "project_file", "cost_center", "approved_by", "rejected_by"]
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
                "cost_center",
                ("external_reference","file"),
                ("payment_method", "payment_terms_days"),
            )
        }),
        (_("Financials & Approval"), {
            "fields": (
                ("total_amount_display", "total_amount_with_tax_display"),
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
    
    inlines = [PurchaseOrderItemInline, PurchaseOrderMessageInline]
    change_form_outer_after_template = "admin/purchasing/purchaseorder/message_form.html"

    conditional_fields = {
        "project_file": "typeof po_type === 'undefined' || po_type == 'PROJECT'",
    }

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj and obj.po_type != PurchaseOrderType.PROJECT and obj.status != PurchaseOrderStatus.DRAFT:
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

    actions_row = ["send_to_approval", "review_approval", "approve_po", "reject_po", "receive_directly", "view_pdf_row_action", "download_pdf_row_action"]
    actions_detail = [
        {
            "title": _("PDF"),
            "icon": "picture_as_pdf",
            "items": [
                "view_pdf_detail_action",
                "download_pdf_detail_action",
            ],
        },
        {
            "title": _("Approval"),
            "icon": "task_alt",
            "items": [
                "send_to_approval_detail_action",
                "approval_analysis_detail_action",
                "approve_po_detail_action",
                "reject_po_detail_action",
            ],
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

    @display(description=_("Date ordered"), ordering="date_ordered")
    def get_date_ordered(self, obj):
        return date_format(obj.date_ordered, "SHORT_DATE_FORMAT") if obj.date_ordered else ""

    def get_queryset(self, request):
        from sales.models import Quote, QuoteStatus
        accepted_quotes = Quote.objects.filter(
            project_file=OuterRef("project_file"),
            status=QuoteStatus.ACCEPTED,
        )
        return super().get_queryset(request).annotate(
            _total_qty=Sum("items__quantity"),
            _total_invoiced=Sum("items__invoiced_quantity"),
            _has_accepted_quote=Exists(accepted_quotes),
        )

    @display(description=_("Margin"))
    def get_projected_margin(self, obj):
        project_file = obj.project_file
        if project_file is None or not getattr(obj, "_has_accepted_quote", False):
            return "-"
        projected_purchased = project_file.total_purchased + obj.total_amount
        margin_after = project_file.total_amount - projected_purchased
        revenue = project_file.total_amount.amount
        if revenue:
            margin_pct = (margin_after.amount / revenue) * 100
        else:
            margin_pct = Decimal("0")
        color = "#22c55e" if margin_after.amount >= 0 else "#ef4444"
        return format_html('<span style="color:{}">{}%</span>', color, round(margin_pct, 1))

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

    @display(description=_("Subtotal"))
    def total_amount_display(self, obj):
        if not obj.pk:
            return "-"
        return obj.total_amount

    @display(description=_("Total Amount"), ordering="total_amount")
    def total_amount_with_tax_display(self, obj):
        if not obj.pk:
            return "-"
        return obj.total_amount_with_tax

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

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        """Feed the Conversation tab and mark its messages as read.

        This hook rather than change_view: Django has already fetched the
        order by the time it calls this, so the thread costs no second lookup.
        """
        if obj is not None and obj.user_can_view_conversation(request.user):
            thread = list(obj.messages.select_related("sender", "recipient"))
            unread_ids = [
                message.pk for message in thread
                if request.user.is_authenticated
                and message.recipient_id == request.user.pk
                and message.read_at is None
            ]
            context.update({
                "po_thread": thread,
                "po_thread_unread_ids": unread_ids,
                # Gates the compose form, which renders outside the inline.
                "can_use_conversation": obj.user_can_post_message(request.user),
            })
            if request.method == "GET" and unread_ids:
                # The thread is rendered on every load, so opening the order
                # is what counts as reading its messages.
                now = timezone.now()
                PurchaseOrderMessage.objects.filter(pk__in=unread_ids).update(
                    read_at=now, updated_at=now
                )
        return super().render_change_form(request, context, add, change, form_url, obj)

    def save_model(self, request, obj, form, change):
        if not getattr(obj, "requested_by", None):
            obj.requested_by = request.user
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != PurchaseOrderStatus.DRAFT:
            return [
                f.name for f in self.model._meta.fields
                if f.name not in ("id", "cost_center")
            ] + list(self.readonly_fields)
        return super().get_readonly_fields(request, obj)

    def _redirect_to_changelist_preserving_filters(self, request):
        """Row actions (the "..." menu) are plain link clicks, so the browser
        sends a Referer with the full changelist URL the user was on,
        filters and all. Bounce back there instead of a bare changelist
        redirect, which used to wipe out any filters the user had applied.
        """
        changelist_url = reverse("admin:purchasing_purchaseorder_changelist")
        referer = request.META.get("HTTP_REFERER")
        if referer and urlparse(referer).path == changelist_url:
            return redirect(referer)
        return redirect(changelist_url)

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
        return self._redirect_to_changelist_preserving_filters(request)

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
        return self._redirect_to_changelist_preserving_filters(request)

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

    @action(description=_("Review"), icon="analytics", permissions=["can_approve_purchase_order", "po_is_pending_approval"])
    def review_approval(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_approval_analysis", args=[object_id]))

    @action(description=_("Review"), icon="analytics", permissions=["can_approve_purchase_order", "po_is_pending_approval"])
    def approval_analysis_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_approval_analysis", args=[object_id]))

    @action(description=_("Register Identity Codes"), icon="qr_code_2")
    def register_identity_codes(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_identity_codes", args=[object_id]))

    @action(description=_("Match Invoice"), icon="link")
    def match_invoice_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_match_invoice", args=[object_id]))

    @action(description=_("View PDF"), icon="visibility", attrs={"target": "_blank"})
    def view_pdf_row_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_pdf", args=[object_id]))

    @action(description=_("View PDF"), icon="visibility", attrs={"target": "_blank"})
    def view_pdf_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_pdf", args=[object_id]))

    @action(description=_("Download PDF"), icon="download")
    def download_pdf_row_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_download_pdf", args=[object_id]))

    @action(description=_("Download PDF"), icon="download")
    def download_pdf_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_purchaseorder_download_pdf", args=[object_id]))

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:po_id>/reject/",
                self.admin_site.admin_view(RejectPurchaseOrderView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_reject",
            ),
            path(
                "<int:po_id>/messages/post/",
                self.admin_site.admin_view(PostPurchaseOrderMessageView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_post_message",
            ),
            path(
                "<int:po_id>/approval-analysis/",
                self.admin_site.admin_view(POApprovalAnalysisView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_approval_analysis",
            ),
            path(
                "<int:po_id>/match-invoice/",
                self.admin_site.admin_view(POMatchInvoiceView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_match_invoice",
            ),
            path(
                "<int:po_id>/ai-match-invoice/",
                self.admin_site.admin_view(POAIMatchView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_ai_match_invoice",
            ),
            path(
                "<int:po_id>/run-ai-match-invoice/",
                self.admin_site.admin_view(DispatchPOAIMatchView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_run_ai_match_invoice",
            ),
            path(
                "<int:po_id>/receive-directly",
                self.admin_site.admin_view(DirectReceiptView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_receive_directly_view",
            ),
            path(
                "<int:po_id>/pdf/",
                self.admin_site.admin_view(PurchaseOrderPdfView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_pdf",
            ),
            path(
                "<int:po_id>/download-pdf/",
                self.admin_site.admin_view(PurchaseOrderPdfDownloadView.as_view(model_admin=self)),
                name="purchasing_purchaseorder_download_pdf",
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


# ---------------------------------------------------------------------------
# Quote Requests (RFQ)
# ---------------------------------------------------------------------------

def _copy_link_button(token):
    """
    Renders a small "Copy Link" button. The path is relative; the button
    prepends window.location.origin at click time, so it works
    regardless of host/port.
    """
    path_ = reverse("purchasing_quote_public_response", args=[token])
    return format_html(
        '<button type="button" '
        'onclick="navigator.clipboard.writeText(window.location.origin + \'{0}\').then(() => {{'
        'const el = this; const original = el.textContent; el.textContent = \'{1}\'; '
        'setTimeout(() => {{ el.textContent = original; }}, 1500); }})" '
        'class="px-2 py-1 text-xs font-medium rounded-md border border-gray-300 hover:bg-gray-50 whitespace-nowrap">'
        '{2}</button>',
        path_, _("Copied!"), _("Copy Link"),
    )


class QuoteRequestItemInline(StackedInline):
    model = QuoteRequestItem
    extra = 1
    tab = True
    autocomplete_fields = ["inventory_item"]
    fieldsets = (
        (None, {
            "fields": (
                ("inventory_item", "unit"),
                ("quantity", "description"),
            )
        }),
    )

    def has_add_permission(self, request, obj=None):
        if obj and obj.status != QuoteRequestStatus.DRAFT:
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj and obj.status != QuoteRequestStatus.DRAFT:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status != QuoteRequestStatus.DRAFT:
            return False
        return super().has_delete_permission(request, obj)


class ProviderQuoteSummaryInline(TabularInline):
    """
    Read-only summary of the ProviderQuotes generated for this
    QuoteRequest, shown directly on the QuoteRequest detail page so the
    user doesn't need to leave it to see progress. Creation, sending and
    deleting happen from the "Create Quotes" screen or from the
    Provider Quote itself.
    """
    model = ProviderQuote
    extra = 0
    tab = True
    fields = ["code_link", "provider", "get_status_label", "total_amount", "copy_link_button"]
    readonly_fields = ["code_link", "provider", "get_status_label", "total_amount", "copy_link_button"]

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @display(description=_("Code"))
    def code_link(self, obj):
        if not obj.pk:
            return "-"
        return format_html(
            '<a href="{}" class="font-mono font-semibold" style="color:#0f766e;">{}</a>',
            reverse("admin:purchasing_providerquote_change", args=[obj.pk]),
            obj.code,
        )

    @display(description=_("Status"))
    def get_status_label(self, obj):
        colors = {
            ProviderQuoteStatus.PENDING: ("#fef3c7", "#92400e"),
            ProviderQuoteStatus.SENT: ("#dbeafe", "#1e40af"),
            ProviderQuoteStatus.RESPONDED: ("#d1fae5", "#065f46"),
            ProviderQuoteStatus.SELECTED: ("#d1fae5", "#065f46"),
            ProviderQuoteStatus.REJECTED: ("#f3f4f6", "#4b5563"),
        }
        bg, fg = colors.get(obj.status, ("#f3f4f6", "#4b5563"))
        with translation.override("en"):
            status_text = obj.get_status_display()
        return format_html(
            '<span style="display:inline-block;padding:2px 10px;border-radius:9999px;'
            'font-size:11px;font-weight:600;background-color:{};color:{};">{}</span>',
            bg, fg, status_text,
        )

    @display(description=_("Link"))
    def copy_link_button(self, obj):
        if not obj.pk:
            return "-"
        return _copy_link_button(obj.token)


@admin.register(QuoteRequest)
class QuoteRequestAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number_display", "project_file", "requested_by", "get_status_label", "date_requested", "get_provider_quotes_count", "company"]
    list_filter = [("status", MultipleChoicesDropdownFilter), ("date_requested", RangeDateFilter), ("company", AutocompleteSelectMultipleFilter)]
    search_fields = ["number", "project_file__reference", "project_file__name", "requested_by__username"]
    autocomplete_fields = ["company", "requested_by", "project_file"]
    readonly_fields = ["number", "status", "awarded_quote", "purchase_order"]
    list_select_related = ["company", "requested_by", "project_file"]
    list_filter_submit = True

    fieldsets = (
        (_("General Information"), {
            "fields": (
                ("number", "company"),
                ("requested_by", "date_requested"),
                "project_file",
                "status",
            )
        }),
        (_("Result"), {
            "fields": (
                ("awarded_quote", "purchase_order"),
            )
        }),
        (_("Notes"), {
            "fields": ("notes",),
        }),
    )

    inlines = [QuoteRequestItemInline, ProviderQuoteSummaryInline]

    actions_row = ["view_pdf_row_action", "download_pdf_row_action"]
    actions_detail = ["create_quotes_detail_action", "compare_quotes_detail_action", "view_pdf_detail_action", "download_pdf_detail_action"]

    @display(description=_("Request Number"), ordering="number")
    def number_display(self, obj):
        return obj.number

    @display(
        description=_("Status"),
        ordering="status",
        label={
            QuoteRequestStatus.DRAFT: "warning",
            QuoteRequestStatus.REQUESTED: "info",
            QuoteRequestStatus.CONVERTED: "success",
        },
    )
    def get_status_label(self, obj):
        with translation.override("en"):
            return obj.status, obj.get_status_display()

    @display(description=_("Provider Quotes"))
    def get_provider_quotes_count(self, obj):
        return obj.provider_quotes.count()

    def save_model(self, request, obj, form, change):
        if not getattr(obj, "requested_by", None):
            obj.requested_by = request.user
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status == QuoteRequestStatus.CONVERTED:
            return [f.name for f in self.model._meta.fields if f.name != "id"] + list(self.readonly_fields)
        return super().get_readonly_fields(request, obj)

    @action(description=_("Create Quotes"), icon="add_circle")
    def create_quotes_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_quoterequest_create_quotes", args=[object_id]))

    @action(description=_("Compare Quotes"), icon="compare_arrows")
    def compare_quotes_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_quoterequest_compare_quotes", args=[object_id]))

    @action(description=_("View PDF"), icon="visibility", attrs={"target": "_blank"})
    def view_pdf_row_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_quoterequest_pdf", args=[object_id]))

    @action(description=_("View PDF"), icon="visibility", attrs={"target": "_blank"})
    def view_pdf_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_quoterequest_pdf", args=[object_id]))

    @action(description=_("Download PDF"), icon="download")
    def download_pdf_row_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_quoterequest_download_pdf", args=[object_id]))

    @action(description=_("Download PDF"), icon="download")
    def download_pdf_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:purchasing_quoterequest_download_pdf", args=[object_id]))

    def get_urls(self):
        from .views import CreateQuotesView, QuoteComparisonView, ProviderAutocompleteView, QuoteRequestPdfView, QuoteRequestPdfDownloadView
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:quote_request_id>/create-quotes/",
                self.admin_site.admin_view(CreateQuotesView.as_view(model_admin=self)),
                name="purchasing_quoterequest_create_quotes",
            ),
            path(
                "<int:quote_request_id>/compare-quotes/",
                self.admin_site.admin_view(QuoteComparisonView.as_view(model_admin=self)),
                name="purchasing_quoterequest_compare_quotes",
            ),
            path(
                "<int:object_id>/pdf/",
                self.admin_site.admin_view(QuoteRequestPdfView.as_view(model_admin=self)),
                name="purchasing_quoterequest_pdf",
            ),
            path(
                "<int:object_id>/download-pdf/",
                self.admin_site.admin_view(QuoteRequestPdfDownloadView.as_view(model_admin=self)),
                name="purchasing_quoterequest_download_pdf",
            ),
            path(
                "autocomplete/providers/",
                self.admin_site.admin_view(ProviderAutocompleteView.as_view()),
                name="purchasing_provider_autocomplete",
            ),
        ]
        return custom_urls + urls


class ProviderQuoteItemInline(TabularInline):
    model = ProviderQuoteItem
    extra = 0
    tab = True
    fields = ["quote_request_item", "description", "quantity", "unit", "unit_price", "line_total"]
    readonly_fields = ["quote_request_item", "description", "quantity", "unit", "line_total"]

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProviderQuote)
class ProviderQuoteAdmin(ModelAdmin):
    list_display = ["code", "quote_request", "provider", "get_status_label", "sent_at", "responded_at", "total_amount", "copy_link_button"]
    list_filter = [("status", MultipleChoicesDropdownFilter), "sent_at"]
    search_fields = ["code", "quote_request__number", "provider__name"]
    autocomplete_fields = ["quote_request", "provider"]
    readonly_fields = ["code", "token", "status", "sent_at", "responded_at", "total_amount", "public_link_display"]
    list_select_related = ["quote_request", "provider"]

    fieldsets = (
        (_("General Information"), {
            "fields": (
                ("code", "quote_request", "provider"),
                ("status", "total_amount"),
                ("sent_at", "responded_at"),
                "public_link_display",
            )
        }),
        (_("Quote Terms"), {
            "fields": (
                ("shipping_cost", "discount_amount"),
                ("valid_until", "delivery_time_days"),
                "provider_notes",
            )
        }),
    )

    inlines = [ProviderQuoteItemInline]

    actions_row = ["send_email_es", "send_email_en", "mark_as_sent"]
    actions_detail = ["send_email_es_detail_action", "send_email_en_detail_action", "mark_as_sent_detail_action", "select_quote_detail_action"]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            ProviderQuoteStatus.PENDING: "warning",
            ProviderQuoteStatus.SENT: "info",
            ProviderQuoteStatus.RESPONDED: "success",
            ProviderQuoteStatus.SELECTED: "success",
            ProviderQuoteStatus.REJECTED: "neutral",
        },
    )
    def get_status_label(self, obj):
        with translation.override("en"):
            return obj.status, obj.get_status_display()

    @display(description=_("Link"))
    def copy_link_button(self, obj):
        if not obj.pk:
            return "-"
        return _copy_link_button(obj.token)

    @display(description=_("Public Link"))
    def public_link_display(self, obj):
        if not obj.pk:
            return "-"
        return _copy_link_button(obj.token)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status not in [ProviderQuoteStatus.PENDING, ProviderQuoteStatus.SENT]:
            return False
        return super().has_delete_permission(request, obj)

    @action(description=_("Send Email (ES)"), url_path="send-email-es-row-action")
    def send_email_es(self, request, object_id: int):
        return self._send_email(request, object_id, "es", redirect_to_changelist=True)

    @action(description=_("Send Email (EN)"), url_path="send-email-en-row-action")
    def send_email_en(self, request, object_id: int):
        return self._send_email(request, object_id, "en", redirect_to_changelist=True)

    @action(description=_("Send Email (ES)"), icon="mail")
    def send_email_es_detail_action(self, request, object_id: int):
        return self._send_email(request, object_id, "es", redirect_to_changelist=False)

    @action(description=_("Send Email (EN)"), icon="mail")
    def send_email_en_detail_action(self, request, object_id: int):
        return self._send_email(request, object_id, "en", redirect_to_changelist=False)

    def _send_email(self, request, object_id, language, redirect_to_changelist):
        quote = ProviderQuote.objects.get(pk=object_id)
        try:
            quote.queue_quote_email(language=language, user=request.user)
        except ValidationError as e:
            self.message_user(request, "; ".join(e.messages), level="error")
        else:
            self.message_user(request, _("Email queued to %(email)s.") % {"email": quote.provider.email})
        if redirect_to_changelist:
            return redirect("admin:purchasing_providerquote_changelist")
        return redirect("admin:purchasing_providerquote_change", object_id)

    @action(description=_("Mark as Sent"), url_path="mark-as-sent-row-action")
    def mark_as_sent(self, request, object_id: int):
        quote = ProviderQuote.objects.get(pk=object_id)
        if quote.status != ProviderQuoteStatus.PENDING:
            self.message_user(request, _("Only a Pending quote can be marked as sent."), level="error")
        else:
            quote.mark_as_sent()
            self.message_user(request, _("Quote marked as sent."))
        return redirect("admin:purchasing_providerquote_changelist")

    @action(description=_("Mark as Sent"), icon="send", permissions=["quote_is_pending"])
    def mark_as_sent_detail_action(self, request, object_id: int):
        quote = ProviderQuote.objects.get(pk=object_id)
        quote.mark_as_sent()
        self.message_user(request, _("Quote marked as sent."))
        return redirect("admin:purchasing_providerquote_change", object_id)

    def has_quote_is_pending_permission(self, request, object_id=None):
        if not object_id:
            return True
        return ProviderQuote.objects.filter(pk=object_id, status=ProviderQuoteStatus.PENDING).exists()

    @action(description=_("Select this Quote"), icon="check_circle")
    def select_quote_detail_action(self, request, object_id: int):
        quote = ProviderQuote.objects.get(pk=object_id)
        try:
            po = quote.convert_to_purchase_order(user=request.user)
        except Exception as e:
            self.message_user(request, str(e), level="error")
            return redirect("admin:purchasing_providerquote_change", object_id)
        self.message_user(request, _("Purchase Order %(number)s created.") % {"number": po.number})
        return redirect("admin:purchasing_purchaseorder_change", po.pk)
