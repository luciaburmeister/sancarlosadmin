from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class QuoteEmail(EmailDocument):
    key = "quote"
    label = _("Quote")


@register
class DeliveryOrderEmail(EmailDocument):
    key = "delivery_order"
    label = _("Delivery Order")


@register
class ClientTermsEmail(EmailDocument):
    key = "client_terms"
    label = _("Client Terms Acceptance")
