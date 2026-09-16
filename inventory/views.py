"""
Inventory stock-movement views — Receive, Transfer, Adjust, InvoiceInfo,
plus bulk identity-code registration, plus read-only breakdowns of the
Incoming / Reserved cached figures.
"""

import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import DecimalField, F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import FormView

from unfold.views import UnfoldModelAdminViewMixin
from unfold.widgets import UnfoldAdminRelatedFieldWrapperWidget

from core.admin.company_access import get_user_companies
from inventory.choices import MovementType
from inventory.forms import (
    UNLINKED,
    AdjustmentMovementForm,
    ItemIdentityCodesForm,
    ReceiptMovementForm,
    TransferMovementForm,
)
from inventory.models import InventoryItem, ItemIdentityCode, ItemTransaction


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
            "stock_on_hand": item.stock_on_hand,
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
            with transaction.atomic():
                ItemTransaction.objects.create(
                    item=item,
                    movement_type=MovementType.ADJUST,
                    quantity_change=data["quantity_change"],
                    warehouse=data["warehouse"],
                    user=request.user,
                    note=data.get("note", ""),
                )

                # Cascade BOM deductions for negative adjustments
                if data["quantity_change"] < 0:
                    item.deduct_bom_components(
                        quantity=abs(data["quantity_change"]),
                        warehouse=data["warehouse"],
                        user=request.user,
                        note=data.get("note", "") or f"BOM adjustment for {item.code}",
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


# ─────────────────────────────────────────────────────────────────────────────
# Reserved / Incoming — read-only breakdowns behind the changelist badges
# ─────────────────────────────────────────────────────────────────────────────
#
# These mirror InventoryItemQuerySet.incoming_expression() /
# reserved_expression() exactly, so the rows shown here always sum to the
# cached figure the user clicked on. See inventory/services/stock_position.py
# for the canonical definitions these follow.

_QUANTITY_FIELD = DecimalField(max_digits=16, decimal_places=3)


def _delivered_quantity_subquery():
    """Per-line delivered quantity: units shipped against one specific
    QuoteInventoryItem line, excluding cancelled delivery orders. Same rule
    as the ``delivered`` subquery inside
    ``InventoryItemQuerySet._reserved_expression``, but correlated to a
    single line (``OuterRef("pk")``) instead of aggregated per item, so it
    can be displayed per row."""
    from sales.models import DeliveryOrderItem, DeliveryOrderStatus, QuoteInventoryItem

    return Coalesce(
        Subquery(
            DeliveryOrderItem.objects
            .filter(
                source_content_type=ContentType.objects.get_for_model(QuoteInventoryItem),
                source_object_id=OuterRef("pk"),
            )
            .exclude(delivery_order__status=DeliveryOrderStatus.CANCELLED)
            .values("source_object_id")
            .annotate(total=Sum("quantity", output_field=_QUANTITY_FIELD))
            .values("total")[:1],
            output_field=_QUANTITY_FIELD,
        ),
        Value(Decimal("0"), output_field=_QUANTITY_FIELD),
    )


def _accepted_quote_lines_with_remaining(inventory_item):
    """Quote lines still reserving ``inventory_item`` directly: accepted
    quotes on open projects, annotated with what has already been delivered
    and what remains. Only lines with something still reserved are
    returned — the same rule ``InventoryItemQuerySet.reserved_expression``
    applies to its own-reservation component, so this always sums to that
    component of the cached ``reserved_quantity``."""
    from management.models import ProjectFileStatus
    from sales.models import QuoteInventoryItem, QuoteStatus

    return (
        QuoteInventoryItem.objects
        .filter(inventory_item=inventory_item, quote__status=QuoteStatus.ACCEPTED)
        .exclude(quote__project_file__status__in=[
            ProjectFileStatus.COMPLETED,
            ProjectFileStatus.CANCELLED,
        ])
        .select_related("quote", "quote__client", "quote__project_file")
        .annotate(delivered_quantity=_delivered_quantity_subquery())
        .annotate(
            remaining_quantity=Greatest(
                F("quantity") - F("delivered_quantity"),
                Value(Decimal("0"), output_field=_QUANTITY_FIELD),
                output_field=_QUANTITY_FIELD,
            )
        )
        .filter(remaining_quantity__gt=0)
        .order_by("-quote__date")
    )


def _kit_reservations(component_item, path_multiplier=Decimal("1"), depth=None):
    """This component's inherited share of reservation from every kit it is
    part of, walked up to ``InventoryItemQuerySet.RESERVED_BOM_DEPTH``
    levels — the same depth the cached ``reserved_quantity`` follows. Each
    kit's own accepted-quote lines come from
    ``_accepted_quote_lines_with_remaining``, then get scaled by the BOM
    quantities on the path from ``component_item`` up to that kit.

    Returns a flat list of dicts, one per (kit, quote line) pair, each
    carrying ``component_share`` — this component's slice of that line's
    remaining reservation.
    """
    from inventory.models import BillOfMaterialsLine, InventoryItemQuerySet

    if depth is None:
        depth = InventoryItemQuerySet.RESERVED_BOM_DEPTH
    if depth <= 0:
        return []

    rows = []
    for bom_line in BillOfMaterialsLine.objects.filter(component=component_item).select_related("parent"):
        kit = bom_line.parent
        multiplier = path_multiplier * bom_line.quantity
        for quote_line in _accepted_quote_lines_with_remaining(kit):
            rows.append({
                "kit": kit,
                "quote_line": quote_line,
                "component_share": multiplier * quote_line.remaining_quantity,
            })
        rows.extend(_kit_reservations(kit, multiplier, depth - 1))
    return rows


class _ReadOnlyItemViewMixin(UnfoldModelAdminViewMixin, View):
    """Shared plumbing for the two read-only breakdown views: company-scoped
    item lookup (same rule as ItemIdentityCodesView.get_item) and the common
    page context."""

    permission_required = ("inventory.view_inventoryitem",)

    def get_item(self):
        qs = InventoryItem.objects.select_related("company")
        if not self.request.user.is_superuser:
            qs = qs.filter(company__in=get_user_companies(self.request.user))
        return get_object_or_404(qs, pk=self.kwargs["object_id"])

    def _base_context(self, request, item):
        return {
            **self.model_admin.admin_site.each_context(request),
            "item": item,
            "opts": InventoryItem._meta,
            "app_label": InventoryItem._meta.app_label,
            "cancel_url": reverse("admin:inventory_inventoryitem_change", args=[item.pk]),
            "has_permission": True,
        }

    def dispatch(self, request, *args, **kwargs):
        if not self.has_permission():
            return self.handle_no_permission()
        return self.get(request, *args, **kwargs)

    def has_permission(self):
        return self.request.user.has_perms(self.get_permission_required())


class IncomingPurchaseOrdersView(_ReadOnlyItemViewMixin):
    """
    GET /inventory/inventoryitem/<pk>/incoming/
    Every open purchase-order line still owed to this item. Filters mirror
    InventoryItemQuerySet.incoming_expression() exactly.
    """
    title = _("Incoming — Purchase Orders")
    template_name = "admin/inventory/incoming_purchase_orders.html"

    def get(self, request, object_id, **kwargs):
        from purchasing.models import PurchaseOrderItem, PurchaseOrderStatus

        item = self.get_item()
        lines = (
            PurchaseOrderItem.objects
            .filter(
                inventory_item=item,
                purchase_order__status=PurchaseOrderStatus.APPROVED,
                quantity__gt=F("received_quantity"),
            )
            .select_related("purchase_order", "purchase_order__provider")
            .annotate(pending_quantity=F("quantity") - F("received_quantity"))
            .order_by("purchase_order__date_ordered")
        )
        request.current_app = self.model_admin.admin_site.name
        context = {
            **self._base_context(request, item),
            "lines": lines,
            "title": _("Incoming — %s") % item,
        }
        return TemplateResponse(request, self.template_name, context)


class ReservedByQuotesView(_ReadOnlyItemViewMixin):
    """
    GET /inventory/inventoryitem/<pk>/reserved/
    Every accepted quote line still holding stock for this item — directly,
    or inherited from a kit it is a component of. Mirrors
    InventoryItemQuerySet.reserved_expression() exactly (own reservation
    plus the BOM-cascaded share, same depth).
    """
    title = _("Reserved — Quotes")
    template_name = "admin/inventory/reserved_by_quotes.html"

    def get(self, request, object_id, **kwargs):
        item = self.get_item()
        direct_lines = _accepted_quote_lines_with_remaining(item)
        kit_rows = _kit_reservations(item)

        request.current_app = self.model_admin.admin_site.name
        context = {
            **self._base_context(request, item),
            "direct_lines": direct_lines,
            "kit_rows": kit_rows,
            "title": _("Reserved — %s") % item,
        }
        return TemplateResponse(request, self.template_name, context)


# ─────────────────────────────────────────────────────────────────────────────
# Identity Codes — bulk registration, one textarea per receipt line
# ─────────────────────────────────────────────────────────────────────────────

class ItemIdentityCodesView(UnfoldModelAdminViewMixin, FormView):
    """Mirror of purchasing.views.identity_codes.IdentityCodesView, pivoted on
    the InventoryItem: lists every receipt line this item came in through and
    takes a paste of codes for each."""

    template_name = "admin/inventory/identity_codes.html"
    form_class = ItemIdentityCodesForm
    title = _("Register Identity Codes")
    permission_required = ("inventory.add_itemidentitycode",)

    def get_item(self):
        qs = InventoryItem.objects.select_related("company")
        if not self.request.user.is_superuser:
            qs = qs.filter(company__in=get_user_companies(self.request.user))
        return get_object_or_404(qs, pk=self.kwargs["object_id"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["item"] = self.get_item()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["form"]
        context["item"] = self.get_item()
        context["receipt_items"] = [
            (ri, form[f"codes_{ri.id}"]) for ri in form.receipt_items
        ]
        context["opts"] = InventoryItem._meta
        return context

    def form_valid(self, form):
        item = self.get_item()
        ri_map = {ri.id: ri for ri in form.receipt_items}
        codes_created = 0

        with transaction.atomic():
            for key, codes in form.parsed_codes.items():
                # key is UNLINKED (None) for the no-purchase-order textarea.
                # Those rows carry no receipt, but registering them is still a
                # statement that the units are physically in — so stamp
                # received_at explicitly and they read as In stock rather than
                # sitting in limbo as merely Registered.
                if key is UNLINKED:
                    defaults = {"receipt_item": None, "received_at": timezone.now()}
                else:
                    receipt_item = ri_map.get(key)
                    if receipt_item is None:
                        continue
                    # save() stamps received_at from the receipt link itself.
                    defaults = {"receipt_item": receipt_item}
                for code in codes:
                    _obj, created = ItemIdentityCode.objects.get_or_create(
                        item=item, code=code, defaults=defaults,
                    )
                    if created:
                        codes_created += 1

        if codes_created:
            messages.success(self.request, _("%(count)d identity codes registered.") % {"count": codes_created})
        else:
            messages.info(self.request, _("No new identity codes were added."))

        if form.skipped_codes:
            messages.warning(self.request, _(
                "%(count)d code(s) were already registered for this item and "
                "were left untouched: %(codes)s"
            ) % {
                "count": len(form.skipped_codes),
                "codes": ", ".join(sorted(set(form.skipped_codes))),
            })

        return redirect(reverse("admin:inventory_item_identity_codes", args=[item.pk]))