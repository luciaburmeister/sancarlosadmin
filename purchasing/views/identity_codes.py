from django.shortcuts import get_object_or_404, redirect
from django.views.generic import FormView
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from unfold.views import UnfoldModelAdminViewMixin
from purchasing.models import PurchaseOrder
from purchasing.forms import IdentityCodesForm
from inventory.models import ItemIdentityCode


class IdentityCodesView(UnfoldModelAdminViewMixin, FormView):
    template_name = "purchasing/identity_codes.html"
    form_class = IdentityCodesForm
    title = _("Register Identity Codes")
    permission_required = ()

    def get_po(self):
        return get_object_or_404(PurchaseOrder, pk=self.kwargs["po_id"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["purchase_order"] = self.get_po()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["form"]
        context["po"] = self.get_po()
        
        context["receipt_items"] = [
            (ri, form[f"codes_{ri.id}"]) for ri in form.receipt_items
        ]
        context["opts"] = PurchaseOrder._meta
        return context

    def form_valid(self, form):
        po = self.get_po()
        ri_map = {ri.id: ri for ri in form.receipt_items}
        codes_created = 0

        for ri_id, codes in form.parsed_codes.items():
            ri = ri_map.get(ri_id)
            if not ri:
                continue
            for code in codes:
                obj, created = ItemIdentityCode.objects.get_or_create(
                    item=ri.po_item.inventory_item,
                    code=code,
                    defaults={"receipt_item": ri},
                )
                if created:
                    codes_created += 1

        if codes_created:
            messages.success(self.request, _("%(count)d identity codes registered.") % {"count": codes_created})
        else:
            messages.info(self.request, _("No new identity codes were added."))

        return redirect(reverse("admin:purchasing_purchaseorder_identity_codes", args=[po.id]))
