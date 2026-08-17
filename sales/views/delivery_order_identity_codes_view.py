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
    delivery order's line items. Also shows codes already linked."""

    title = _("Link Item Codes")
    permission_required = ("sales.change_deliveryorder",)
    template_name = "admin/sales/deliveryorder/link_identity_codes.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        do = get_object_or_404(
            DeliveryOrder.objects.select_related("company"),
            pk=self.kwargs["delivery_order_id"],
        )

        search = self.request.GET.get("q", "").strip()
        candidates = ItemIdentityCode.objects.filter(
            item__company=do.company,
            delivery_order_item__isnull=True,
        ).select_related("item")
        if search:
            candidates = candidates.filter(
                Q(code__icontains=search)
                | Q(item__name__icontains=search)
                | Q(item__code__icontains=search)
            )
        candidates = candidates.order_by("item__name", "code")[:CANDIDATE_LIMIT]

        linked = ItemIdentityCode.objects.filter(
            delivery_order_item__delivery_order=do
        ).select_related("item", "delivery_order_item").order_by(
            "delivery_order_item_id", "code"
        )

        linked_counts = {
            row["delivery_order_item"]: row["count"]
            for row in ItemIdentityCode.objects.filter(
                delivery_order_item__delivery_order=do
            ).values("delivery_order_item").annotate(count=Count("id"))
        }
        line_items = []
        for line in do.items.all():
            line_items.append({
                "obj": line,
                "linked_count": linked_counts.get(line.pk, 0),
            })

        context.update({
            "delivery_order": do,
            "line_items": line_items,
            "candidates": candidates,
            "candidate_limit": CANDIDATE_LIMIT,
            "linked": linked,
            "search": search,
            "back_url": reverse("admin:sales_deliveryorder_change", args=[do.pk]),
        })
        return context

    def post(self, request, delivery_order_id):
        do = get_object_or_404(DeliveryOrder, pk=delivery_order_id)

        target_item_id = request.POST.get("target_item")
        code_ids = request.POST.getlist("codes")

        if not target_item_id or not code_ids:
            messages.error(request, _("Select a line item and at least one code."))
            return redirect(request.path)

        target_item = do.items.filter(pk=target_item_id).first()
        if not target_item:
            messages.error(request, _("Invalid line item."))
            return redirect(request.path)

        # Codes are discrete, serialized units — a line's quantity caps how
        # many can be traced to it. Fractional quantities (sold by length,
        # weight, etc.) aren't countable this way, so the cap is skipped.
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
                return redirect(request.path)

        codes = ItemIdentityCode.objects.filter(
            pk__in=code_ids,
            item__company=do.company,
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

        return redirect(request.path)


class DeliveryOrderUnlinkIdentityCodeView(UnfoldModelAdminViewMixin, View):
    """POST handler — remove a code's link to this delivery order."""

    permission_required = ("sales.change_deliveryorder",)

    def post(self, request, delivery_order_id, code_id):
        do = get_object_or_404(DeliveryOrder, pk=delivery_order_id)
        code = get_object_or_404(
            ItemIdentityCode, pk=code_id, delivery_order_item__delivery_order=do
        )
        code.delivery_order_item = None
        code.save(update_fields=["delivery_order_item", "delivered_at"])
        messages.info(request, _("Code unlinked from this delivery order."))
        return redirect(
            reverse("admin:sales_deliveryorder_link_identity_codes", args=[delivery_order_id])
        )
