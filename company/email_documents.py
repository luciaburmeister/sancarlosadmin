from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class DocumentReminderEmail(EmailDocument):
    key = "document_reminder"
    label = _("Document Expiration Reminder")
