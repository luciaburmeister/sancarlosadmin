from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView
from unfold.views import UnfoldModelAdminViewMixin

from purchasing.forms import RejectPurchaseOrderForm
from purchasing.models import PurchaseOrder, PurchaseOrderStatus


class RejectPurchaseOrderView(UnfoldModelAdminViewMixin, FormView):
    template_name = "purchasing/reject_po.html"
    form_class = RejectPurchaseOrderForm
    title = _("Reject Purchase Order")
    permission_required = ("purchasing.can_approve_purchase_order",)

    def get_po(self):
        return get_object_or_404(
            self.model_admin.get_queryset(self.request), pk=self.kwargs["po_id"]
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["po"] = self.get_po()
        context["opts"] = PurchaseOrder._meta
        return context

    def form_valid(self, form):
        from purchasing.tasks import send_purchase_order_decision_notification

        with transaction.atomic():
            po = PurchaseOrder.objects.select_for_update().get(pk=self.get_po().pk)
            if po.status != PurchaseOrderStatus.PENDING_APPROVAL:
                messages.error(self.request, _("Only Pending POs can be rejected."))
                return redirect(reverse("admin:purchasing_purchaseorder_change", args=[po.pk]))

            po.status = PurchaseOrderStatus.REJECTED
            po.rejected_by = self.request.user
            po.rejected_at = timezone.now()
            po.rejection_reason = form.cleaned_data["reason"]
            po.automatic_approval_due_at = None
            po.save(update_fields=[
                "status", "rejected_by", "rejected_at", "rejection_reason", "automatic_approval_due_at"
            ])

            transaction.on_commit(
                lambda: send_purchase_order_decision_notification.delay(po.pk, "rejected")
            )

        messages.success(self.request, _("Purchase Order rejected."))
        return redirect(reverse("admin:purchasing_purchaseorder_change", args=[po.pk]))
