from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class AssetTransactionEmail(EmailDocument):
    key = "asset_transaction"
    label = _("Asset Transaction Receipt")


@register
class AssetPlanningRequestEmail(EmailDocument):
    key = "asset_planning_request"
    label = _("Asset Planning Request")


@register
class AssetPlanningDraftEmail(EmailDocument):
    key = "asset_planning_draft"
    label = _("Asset Planning Draft")
