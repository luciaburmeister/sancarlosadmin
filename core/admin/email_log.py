from django.utils.translation import gettext_lazy as _
from unfold.admin import GenericTabularInline

from company.models import EmailLog


class EmailLogInline(GenericTabularInline):
    """Read-only "Emails Sent" tab for any document emailed via EmailService."""

    model = EmailLog
    extra = 0
    tab = True
    verbose_name = _("Email Sent")
    verbose_name_plural = _("Emails Sent")
    fields = (
        "to_email", "subject", "sent_at", "status", "delivery_status",
        "delivered_at", "first_opened_at", "last_opened_at", "open_count",
        "delivery_error", "error_message",
    )
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False
