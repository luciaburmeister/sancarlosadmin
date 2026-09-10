from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class A3ExportEmail(EmailDocument):
    key = "a3_export"
    label = _("A3 Accounting Export")
