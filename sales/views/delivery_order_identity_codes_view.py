"""Link/unlink inventory identity codes to a delivery order's line items,
for full traceability of which physical units were shipped in which order.
"""
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from inventory.models import ItemIdentityCode
from sales.models import DeliveryOrder

CANDIDATE_LIMIT = 200


class DeliveryOrderLinkIdentityCodesView(UnfoldModelAdminViewMixin, TemplateView):
    """Search unlinked identity codes and assign them to one of this
    delivery order's line items. Also shows codes already linked.

    Candidates are scoped to the InventoryItem behind the line item
    currently selected in "Assign selected codes to" — otherwise every
    unlinked code across every model in the company would be listed,
    making manual selection impractical.
    """

    title = _("Link Item Codes")
    permission_required = ("sales.change_deliveryorder",)
    template_name = "admin/sales/deliveryorder/link_identity_codes.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        delivery_order = get_object_or_404(
            DeliveryOrder.objects.select_related("company"),
            pk=self.kwargs["delivery_order_id"],
        )

        search_term = self.request.GET.get("q", "").strip()
        target_item_id = self.request.GET.get("target_item", "").strip()

        linked_counts = {
            row["delivery_order_item"]: row["count"]
            for row in ItemIdentityCode.objects.filter(
                delivery_order_item__delivery_order=delivery_order
            ).values("delivery_order_item").annotate(count=Count("id"))
        }

        selectable_line_items = self._selectable_line_items(delivery_order, linked_counts)

        selected_line_item = next(
            (
                entry["obj"] for entry in selectable_line_items
                if str(entry["obj"].pk) == target_item_id
            ),
            None,
        ) if target_item_id else None

        if selected_line_item is not None:
            candidates = ItemIdentityCode.objects.filter(
                item=selected_line_item.inventory_item,
                delivery_order_item__isnull=True,
            ).select_related("item")
            if search_term:
                candidates = candidates.filter(
                    Q(code__icontains=search_term)
                    | Q(item__name__icontains=search_term)
                    | Q(item__code__icontains=search_term)
                )
            candidates = candidates.order_by("code")[:CANDIDATE_LIMIT]
        else:
            candidates = ItemIdentityCode.objects.none()

        linked = ItemIdentityCode.objects.filter(
            delivery_order_item__delivery_order=delivery_order
        ).select_related("item", "delivery_order_item").order_by(
            "delivery_order_item_id", "code"
        )

        context.update({
            "delivery_order": delivery_order,
            "line_items": selectable_line_items,
            "selected_line_item": selected_line_item,
            "candidates": candidates,
            "candidate_limit": CANDIDATE_LIMIT,
            "linked": linked,
            "search": search_term,
            "back_url": reverse("admin:sales_deliveryorder_change", args=[delivery_order.pk]),
        })
        return context

    @staticmethod
    def _selectable_line_items(delivery_order, linked_counts):
        """Lines that ship an inventory item, in document order: each
        top-level line followed by its component lines, nested ones
        indented one level deeper."""
        lines = list(
            delivery_order.items.select_related("inventory_item").order_by("pk")
        )
        children = {}
        for line in lines:
            children.setdefault(line.parent_line_id, []).append(line)

        entries = []

        def walk(parent_id, depth):
            for line in children.get(parent_id, []):
                if line.inventory_item_id is not None:
                    entries.append({
                        "obj": line,
                        "depth": depth,
                        "indent": " " * depth,
                        "linked_count": linked_counts.get(line.pk, 0),
                    })
                walk(line.pk, depth + 1)

        walk(None, 0)
        return entries

    def post(self, request, delivery_order_id):
        delivery_order = get_object_or_404(DeliveryOrder, pk=delivery_order_id)

        target_item_id = request.POST.get("target_item", "").strip()
        code_ids = request.POST.getlist("codes")

        if not target_item_id or not code_ids:
            messages.error(request, _("Select a line item and at least one code."))
            return redirect(request.path)

        target_item = delivery_order.items.filter(pk=target_item_id).first()
        if not target_item or target_item.inventory_item is None:
            messages.error(request, _("Invalid line item."))
            return redirect(request.path)


        redirect_url = f"{request.path}?target_item={target_item_id}"


        quantity = target_item.quantity
        if quantity == quantity.to_integral_value():
            capacity = int(quantity)
            already_linked = ItemIdentityCode.objects.filter(
                delivery_order_item=target_item
            ).count()
            if already_linked + len(code_ids) > capacity:
                remaining = max(0, capacity - already_linked)
                messages.error(
                    request,
                    _(
                        "%(item)s has a quantity of %(qty)d — %(linked)d code(s) "
                        "already linked, only %(remaining)d more can be added."
                    )
                    % {
                        "item": target_item,
                        "qty": capacity,
                        "linked": already_linked,
                        "remaining": remaining,
                    },
                )
                return redirect(redirect_url)

        # Scoped to the target line's own InventoryItem — not just the
        # company — so a tampered request can't link mismatched codes.
        codes = ItemIdentityCode.objects.filter(
            pk__in=code_ids,
            item=target_item.inventory_item,
            delivery_order_item__isnull=True,
        )
        count = 0
        for code in codes:
            code.delivery_order_item = target_item
            code.save(update_fields=["delivery_order_item", "delivered_at"])
            count += 1

        if count:
            messages.success(
                request,
                _("%(count)d code(s) linked to %(item)s.")
                % {"count": count, "item": target_item},
            )
        else:
            messages.warning(request, _("No codes were linked — they may already be taken."))

        return redirect(redirect_url)


class DeliveryOrderUnlinkIdentityCodeView(UnfoldModelAdminViewMixin, View):
    """POST handler — remove a code's link to this delivery order."""

    permission_required = ("sales.change_deliveryorder",)

    def post(self, request, delivery_order_id, code_id):
        delivery_order = get_object_or_404(DeliveryOrder, pk=delivery_order_id)
        code = get_object_or_404(
            ItemIdentityCode, pk=code_id, delivery_order_item__delivery_order=delivery_order
        )
        code.delivery_order_item = None
        code.save(update_fields=["delivery_order_item", "delivered_at"])
        messages.info(request, _("Code unlinked from this delivery order."))
        return redirect(
            reverse("admin:sales_deliveryorder_link_identity_codes", args=[delivery_order_id])
        )