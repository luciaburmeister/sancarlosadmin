from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import View
from unfold.views import UnfoldModelAdminViewMixin

from purchasing.forms import PurchaseOrderMessageForm
from purchasing.models import PurchaseOrderMessage


class PostPurchaseOrderMessageView(UnfoldModelAdminViewMixin, View):
    """
    Receives a message written in the Conversation tab of the PO change page.

    The tab cannot submit through the change form: its compose box is bound to
    this URL instead, so writing a message neither requires change permission
    on the order nor re-saves it.
    """
    title = _("Conversation")
    permission_required = ("purchasing.view_purchaseorder",)
    http_method_names = ["post"]

    def get_po(self):
        # Through the admin's queryset, so another company's order is not
        # reachable by pk.
        try:
            return self.model_admin.get_queryset(self.request).get(pk=self.kwargs["po_id"])
        except self.model_admin.model.DoesNotExist:
            raise Http404

    def post(self, request, *args, **kwargs):
        from purchasing.tasks import send_purchase_order_message_notification

        po = self.get_po()
        if not po.user_can_post_message(request.user):
            raise PermissionDenied

        redirect_url = reverse("admin:purchasing_purchaseorder_change", args=[po.pk])
        form = PurchaseOrderMessageForm(request.POST)
        if not form.is_valid():
            messages.error(request, _("The message cannot be empty."))
            return redirect(f"{redirect_url}#messages")

        message = PurchaseOrderMessage.objects.create(
            purchase_order=po,
            sender=request.user,
            body=form.cleaned_data["body"],
        )
        transaction.on_commit(
            lambda: send_purchase_order_message_notification.delay(message.pk)
        )

        messages.success(request, _("Message sent."))
        return HttpResponseRedirect(f"{redirect_url}#messages")
