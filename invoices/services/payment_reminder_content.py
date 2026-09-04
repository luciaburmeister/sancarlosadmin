from django.utils.translation import gettext as _t

from invoices.models import PaymentReminderLanguage, PaymentReminderType

REMINDER_SUBJECTS = {
    PaymentReminderLanguage.ES: {
        PaymentReminderType.PRE_DUE: _t("Recordatorio: su factura %(number)s vence próximamente"),
        PaymentReminderType.OVERDUE: _t("Aviso: su factura %(number)s se encuentra vencida"),
    },
    PaymentReminderLanguage.EN: {
        PaymentReminderType.PRE_DUE: _t("Reminder: your invoice %(number)s is due soon"),
        PaymentReminderType.OVERDUE: _t("Notice: your invoice %(number)s is overdue"),
    },
}


def get_reminder_subject(language: str, reminder_type: str, invoice_number: str) -> str:
    """
    Returns the email subject for a payment reminder, given the resolved
    language (see invoices.models.resolve_reminder_language) and the
    reminder type (pre-due vs. overdue).
    """
    return REMINDER_SUBJECTS[language][reminder_type] % {"number": invoice_number}


def get_reminder_template(language: str) -> str:
    """
    Returns the template path for a payment reminder email in the given
    language. Add new languages here (and to PaymentReminderLanguage)
    together with the matching template file.
    """
    return f"email/payment_reminder_{language}.html"


CONSOLIDATED_SUBJECTS = {
    PaymentReminderLanguage.ES: _t("Recordatorio: tiene %(count)s facturas pendientes de pago"),
    PaymentReminderLanguage.EN: _t("Reminder: you have %(count)s pending invoices"),
}


def get_consolidated_reminder_subject(language: str, invoice_count: int) -> str:
    """
    Subject for a manual consolidated reminder covering several invoices
    of the same client in a single email.
    """
    return CONSOLIDATED_SUBJECTS[language] % {"count": invoice_count}


def get_consolidated_reminder_template(language: str) -> str:
    return f"email/payment_reminder_consolidated_{language}.html"