from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class ProviderQuoteEmail(EmailDocument):
    key = "provider_quote"
    label = _("Provider Quote Request")


@register
class PurchaseOrderApprovalEmail(EmailDocument):
    key = "purchase_order_approval"
    label = _("Purchase Order Pending Approval")


@register
class PurchaseOrderDecisionEmail(EmailDocument):
    key = "purchase_order_decision"
    label = _("Purchase Order Decision")
