from django.views.generic.edit import FormView
from django.shortcuts import redirect
from django.contrib import messages
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from unfold.views import UnfoldModelAdminViewMixin

from apps.expenses.forms import ExpenseTicketUploadForm
from apps.expenses.tasks import process_expense_ticket
from core.admin.company_access import get_user_companies


class ExpenseTicketUploadAdminView(UnfoldModelAdminViewMixin, FormView):
    form_class = ExpenseTicketUploadForm
    title = _("Upload Expense Receipt")
    permission_required = ("expenses.add_expenseticket",)
    template_name = "admin/expenses/expenseticket_upload.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Pre-set company and submitted_by on the instance so clean() can
        # use company_id for the duplicate check before the form is saved.
        form.instance.submitted_by = self.request.user
        company = get_user_companies(self.request.user).first()
        form.instance.company = company
        return form

    def form_valid(self, form):
        ticket = form.save()
        process_expense_ticket.delay(ticket.id)
        messages.success(
            self.request,
            _("Receipt uploaded. We're extracting the information with AI in the background."),
        )
        return redirect(reverse("admin:expenses_expenseticket_changelist"))
