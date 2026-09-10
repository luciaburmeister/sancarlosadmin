from django.utils.translation import gettext_lazy as _

from general.email import EmailDocument, register


@register
class DuplicateExpenseTicketEmail(EmailDocument):
    key = "duplicate_expense_ticket"
    label = _("Duplicate Expense Receipt Alert")
