"""
Inventory stock-movement views — Receive, Transfer, Adjust, InvoiceInfo.
"""

import datetime

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from unfold.views import UnfoldModelAdminViewMixin
from unfold.widgets import UnfoldAdminRelatedFieldWrapperWidget

from inventory.choices import MovementType
from inventory.forms import AdjustmentMovementForm, ReceiptMovementForm, TransferMovementForm
from inventory.models import InventoryItem, ItemTransaction


# ─────────────────────────────────────────────────────────────────────────────
# FK relation helper
# ─────────────────────────────────────────────────────────────────────────────

def _wrap_related(form, field_name, remote_field, admin_site, user):
    field = form.fields.get(field_name)
    if field is None:
        return

    related_model = remote_field.model
    opts = related_model._meta
    can_add    = user.has_perm(f"{opts.app_label}.add_{opts.model_name}")
    can_change = user.has_perm(f"{opts.app_label}.change_{opts.model_name}")
    can_delete = user.has_perm(f"{opts.app_label}.delete_{opts.model_name}")
    can_view   = user.has_perm(f"{opts.app_label}.view_{opts.model_name}")

    field.widget = UnfoldAdminRelatedFieldWrapperWidget(
        widget=field.widget,
        rel=remote_field,
        admin_site=admin_site,
        can_add_related=can_add,
        can_change_related=can_change,
        can_delete_related=can_delete,
        can_view_related=can_view,
    )


def _apply_related_wrappers(form, admin_site, user):
    from inventory.models import ItemTransaction, InventoryItem

    # Warehouse FK
    wh_rel = ItemTransaction._meta.get_field("warehouse").remote_field
    for fname in ("warehouse", "source_warehouse", "destination_warehouse"):
        _wrap_related(form, fname, wh_rel, admin_site, user)

    # Supplier (Provider) FK
    supplier_rel = InventoryItem._meta.get_field("preferred_supplier").remote_field
    _wrap_related(form, "supplier", supplier_rel, admin_site, user)

    # Invoice FK — uses real remote_field from InventoryItem.invoice
    invoice_rel = InventoryItem._meta.get_field("invoice").remote_field
    _wrap_related(form, "invoice", invoice_rel, admin_site, user)


# ─────────────────────────────────────────────────────────────────────────────
# Shared mixin
# ─────────────────────────────────────────────────────────────────────────────

class _MovementViewMixin(UnfoldModelAdminViewMixin, View):

    permission_required = ("inventory.change_inventoryitem",)

    def _get_item(self):
        return get_object_or_404(InventoryItem, pk=self.kwargs["object_id"])

    def _real_stock(self, item):
        return (
            ItemTransaction.objects
            .filter(item=item)
            .aggregate(total=models.Sum("quantity_change"))
            .get("total") or 0
        )

    def _cancel_url(self, object_id):
        return reverse("admin:inventory_inventoryitem_change", args=[object_id])

    def _wrap_form(self, form):
        _apply_related_wrappers(form, self.model_admin.admin_site, self.request.user)
        return form

    def _build_media(self, form):
        return self.model_admin.media + form.media

    def _base_context(self, item, form):
        object_id = self.kwargs["object_id"]
        return {
            "item": item,
            "form": form,
            "stock_on_hand": self._real_stock(item),
            "cancel_url": self._cancel_url(object_id),
            "opts": InventoryItem._meta,
            "app_label": InventoryItem._meta.app_label,
            "has_permission": True,
            "media": self._build_media(form),
        }

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs)

    def dispatch(self, request, *args, **kwargs):
        if not self.has_permission():
            return self.handle_no_permission()
        if request.method == "POST":
            return self.post(request, *args, **kwargs)
        return self.get(request, *args, **kwargs)

    def has_permission(self):
        return self.request.user.has_perms(self.get_permission_required())


# ─────────────────────────────────────────────────────────────────────────────
# Invoice Info — AJAX endpoint used by the receive form to autocomplete supplier
# ─────────────────────────────────────────────────────────────────────────────

class InvoiceInfoView(_MovementViewMixin):
    """
    GET /inventory/inventoryitem/<pk>/invoice-info/?invoice_id=<id>
    Returns JSON: { provider_id, provider_name, invoice_number, date_issued }
    Used by the Receive Stock form to auto-fill the supplier when an invoice
    is selected.
    """
    permission_required = ("inventory.change_inventoryitem",)

    def get(self, request, object_id, **kwargs):
        invoice_id = request.GET.get("invoice_id")
        if not invoice_id:
            return JsonResponse({})
        try:
            from invoices.models import Invoice
            invoice = Invoice.objects.select_related("provider").get(pk=invoice_id)
            return JsonResponse({
                "provider_id":     invoice.provider_id or "",
                "provider_name":   str(invoice.provider) if invoice.provider else "",
                "invoice_number":  invoice.invoice_number or "",
                "date_issued":     invoice.date_issued.isoformat() if invoice.date_issued else "",
            })
        except Exception:
            return JsonResponse({})


# ─────────────────────────────────────────────────────────────────────────────
# Receive Stock
# ─────────────────────────────────────────────────────────────────────────────

class ReceiveStockView(_MovementViewMixin):
    title = _("Receive Stock")
    permission_required = ("inventory.change_inventoryitem",)
    template_name = "admin/inventory/movement_receive.html"

    def get(self, request, object_id, **kwargs):
        item = self._get_item()
        form = self._wrap_form(ReceiptMovementForm(initial={
            "warehouse": item.default_warehouse,
            "supplier":  item.preferred_supplier,
            "received_date": datetime.date.today(),
        }))
        return self._render(request, item, form)

    def post(self, request, object_id, **kwargs):
        item = self._get_item()
        form = ReceiptMovementForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            invoice  = data.get("invoice")
            supplier = data.get("supplier")

            # If no supplier was manually chosen, fall back to the invoice's provider
            if not supplier and invoice and invoice.provider:
                supplier = invoice.provider

            # Link the ItemTransaction back to the Invoice via the generic FK
            ref_ct = ref_id = None
            if invoice:
                from invoices.models import Invoice
                ref_ct = ContentType.objects.get_for_model(Invoice)
                ref_id = invoice.pk

            ItemTransaction.objects.create(
                item=item,
                movement_type=MovementType.IN,
                quantity_change=data["quantity"],
                warehouse=data["warehouse"],
                user=request.user,
                note=data.get("note", ""),
                reference_content_type=ref_ct,
                reference_object_id=ref_id,
            )
            messages.success(
                request,
                _("%(qty)d unit(s) of %(item)s received into %(wh)s.")
                % {"qty": data["quantity"], "item": item.name, "wh": data["warehouse"]},
            )
            return redirect("admin:inventory_inventoryitem_change", object_id)
        return self._render(request, item, self._wrap_form(form))

    def _render(self, request, item, form):
        request.current_app = self.model_admin.admin_site.name
        # Build the AJAX URL so the template can call it without hardcoding
        invoice_info_url = reverse(
            "admin:inventory_item_invoice_info",
            args=[self.kwargs["object_id"]],
        )
        context = {
            **self.model_admin.admin_site.each_context(request),
            **self._base_context(item, form),
            "title": _("Receive Stock — %s") % item,
            "movement_icon": "download",
            "invoice_info_url": invoice_info_url,
        }
        return TemplateResponse(request, self.template_name, context)


# ─────────────────────────────────────────────────────────────────────────────
# Transfer Stock
# ─────────────────────────────────────────────────────────────────────────────

class TransferStockView(_MovementViewMixin):
    title = _("Transfer Stock")
    permission_required = ("inventory.change_inventoryitem",)
    template_name = "admin/inventory/movement_transfer.html"

    def get(self, request, object_id, **kwargs):
        item = self._get_item()
        form = self._wrap_form(TransferMovementForm(
            initial={"source_warehouse": item.default_warehouse}
        ))
        return self._render(request, item, form)

    def post(self, request, object_id, **kwargs):
        item = self._get_item()
        form = TransferMovementForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            with transaction.atomic():
                ItemTransaction.objects.create(
                    item=item,
                    movement_type=MovementType.TRANSFER,
                    quantity_change=-data["quantity"],
                    warehouse=data["source_warehouse"],
                    user=request.user,
                    note=data.get("note", ""),
                )
                ItemTransaction.objects.create(
                    item=item,
                    movement_type=MovementType.TRANSFER,
                    quantity_change=data["quantity"],
                    warehouse=data["destination_warehouse"],
                    user=request.user,
                    note=data.get("note", ""),
                )
            messages.success(
                request,
                _("%(qty)d unit(s) transferred from %(src)s → %(dst)s.")
                % {
                    "qty": data["quantity"],
                    "src": data["source_warehouse"],
                    "dst": data["destination_warehouse"],
                },
            )
            return redirect("admin:inventory_inventoryitem_change", object_id)
        return self._render(request, item, self._wrap_form(form))

    def _render(self, request, item, form):
        request.current_app = self.model_admin.admin_site.name
        context = {
            **self.model_admin.admin_site.each_context(request),
            **self._base_context(item, form),
            "title": _("Transfer Stock — %s") % item,
            "movement_icon": "swap_horiz",
        }
        return TemplateResponse(request, self.template_name, context)


# ─────────────────────────────────────────────────────────────────────────────
# Adjust Stock
# ─────────────────────────────────────────────────────────────────────────────

class AdjustStockView(_MovementViewMixin):
    title = _("Adjust Stock")
    permission_required = ("inventory.change_inventoryitem",)
    template_name = "admin/inventory/movement_adjust.html"

    def get(self, request, object_id, **kwargs):
        item = self._get_item()
        form = self._wrap_form(AdjustmentMovementForm(
            initial={"warehouse": item.default_warehouse}
        ))
        return self._render(request, item, form)

    def post(self, request, object_id, **kwargs):
        item = self._get_item()
        form = AdjustmentMovementForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            ItemTransaction.objects.create(
                item=item,
                movement_type=MovementType.ADJUST,
                quantity_change=data["quantity_change"],
                warehouse=data["warehouse"],
                user=request.user,
                note=data.get("note", ""),
            )
            sign = "+" if data["quantity_change"] > 0 else ""
            messages.success(
                request,
                _("Stock adjusted by %(sign)s%(qty)d for %(item)s in %(wh)s.")
                % {
                    "sign": sign,
                    "qty": data["quantity_change"],
                    "item": item.name,
                    "wh": data["warehouse"],
                },
            )
            return redirect("admin:inventory_inventoryitem_change", object_id)
        return self._render(request, item, self._wrap_form(form))

    def _render(self, request, item, form):
        request.current_app = self.model_admin.admin_site.name
        context = {
            **self.model_admin.admin_site.each_context(request),
            **self._base_context(item, form),
            "title": _("Adjust Stock — %s") % item,
            "movement_icon": "balance",
        }
        return TemplateResponse(request, self.template_name, context)