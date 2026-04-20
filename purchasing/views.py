from django.shortcuts import get_object_or_404, redirect
from django.views.generic import FormView
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.db import transaction
from django.urls import reverse
from unfold.views import UnfoldModelAdminViewMixin
from django.core.exceptions import ValidationError
from .models import PurchaseOrder, PurchaseOrderReceipt, PurchaseOrderReceiptItem, PurchaseOrderStatus
from .forms import DirectReceiptForm
from decimal import Decimal

class DirectReceiptView(UnfoldModelAdminViewMixin, FormView):
    template_name = "purchasing/direct_receipt.html"
    form_class = DirectReceiptForm
    title = _("Receive Items Directly")
    permission_required = ("purchasing.add_purchaseorderreceipt",)

    def dispatch(self, request, *args, **kwargs):
        po_id = self.kwargs.get("po_id")
        if not PurchaseOrder.objects.filter(pk=po_id, status=PurchaseOrderStatus.APPROVED).exists():
            messages.error(request, _("Only APPROVED Purchase Orders can receive items."))
            return redirect("admin:purchasing_purchaseorder_change", po_id)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        po_id = self.kwargs.get("po_id")
        po = PurchaseOrder.objects.get(pk=po_id)
        
        items = po.items.all()
        for item in items:
            item.pending_qty = item.quantity - item.received_quantity
        
        context.update({
            "po": po,
            "items": items,
            "opts": PurchaseOrder._meta,
        })
        return context

    def form_valid(self, form):
        po_id = self.kwargs.get("po_id")
        po = get_object_or_404(PurchaseOrder, pk=po_id)
        
        try:
            with transaction.atomic():
                warehouse = form.cleaned_data["warehouse"]
                receipt = PurchaseOrderReceipt.objects.create(
                    purchase_order=po,
                    warehouse=warehouse,
                    company=po.company,
                    received_by=self.request.user
                )
                
                items_received = 0
                for key, value in self.request.POST.items():
                    if key.startswith("qty_") and value:
                        try:
                            item_id = int(key.replace("qty_", ""))
                            qty_val = Decimal(value.replace(",", "."))
                            
                            if qty_val > 0:
                                # Verify the item belongs to this PO
                                po_item = po.items.filter(pk=item_id).first()
                                if po_item:
                                    PurchaseOrderReceiptItem.objects.create(
                                        receipt=receipt,
                                        po_item=po_item,
                                        quantity_received=qty_val
                                    )
                                    items_received += 1
                        except (ValueError, TypeError, Decimal.InvalidOperation):
                            continue
                
                if items_received > 0:
                    messages.success(self.request, _("Receipt %(number)s created successfully with %(count)d items.") % {'number': receipt.number, 'count': items_received})
                else:
                    receipt.delete()
                    messages.warning(self.request, _("No quantities were entered. No receipt was created."))
        except ValidationError as e:
            messages.error(self.request, ", ".join(e.messages))
            return self.form_invalid(form)
        except Exception as e:
            messages.error(self.request, _("An error occurred while saving the receipt: %(error)s") % {'error': str(e)})
            return self.form_invalid(form)
                
        return redirect("admin:purchasing_purchaseorder_change", po.id)
