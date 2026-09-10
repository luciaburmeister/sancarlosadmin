from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class BankSummaryEmail(EmailDocument):
    key = "bank_summary"
    label = _("Bank Balance Summary")
