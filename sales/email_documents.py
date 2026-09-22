from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class QuoteEmail(EmailDocument):
    key = "quote"
    label = _("Quote")
    track_delivery = True
    track_opens = True


@register
class DeliveryOrderEmail(EmailDocument):
    key = "delivery_order"
    label = _("Delivery Order")
    track_delivery = True
    track_opens = True


@register
class ClientTermsEmail(EmailDocument):
    key = "client_terms"
    label = _("Client Terms Acceptance")
    track_delivery = True
