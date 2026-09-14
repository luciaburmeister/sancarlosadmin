"""
Delivery Order Send Email view — recipient picker with CC support.

Thin subclass of the reusable BaseSendDocumentEmailView (core), which
provides the recipient picker (client email + contacts) and CCs the sender.
"""

from django.utils.translation import gettext_lazy as _

from core.tasks import send_delivery_order_email_task
from core.views.send_document_email_view import BaseSendDocumentEmailView
from sales.models import DeliveryOrder


class DeliveryOrderSendEmailView(BaseSendDocumentEmailView):
    title = _("Send Delivery Order Email")
    permission_required = ("sales.change_deliveryorder",)
    model = DeliveryOrder
    pk_url_kwarg = "delivery_order_id"

    def queue_email(self, obj, to_emails, cc_emails, reply_to, template_id=None):
        send_delivery_order_email_task.delay(
            obj.id,
            to_emails=to_emails,
            cc_emails=cc_emails,
            reply_to=reply_to,
        )
