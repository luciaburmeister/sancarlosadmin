from django.core.exceptions import PermissionDenied
from django.views.generic.edit import FormView
from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from unfold.views import UnfoldModelAdminViewMixin

from apps.expenses.forms import ExpenseClaimAddTicketForm
from apps.expenses.models import ExpenseClaim, ExpenseClaimStatus, ExpenseTicket


class ExpenseClaimAddTicketAdminView(UnfoldModelAdminViewMixin, FormView):
    form_class = ExpenseClaimAddTicketForm
    title = _("Add Ticket to Claim")
    permission_required = ("expenses.change_expenseclaim",)
    template_name = "admin/expenses/expenseclaim_add_ticket.html"

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.claim = get_object_or_404(ExpenseClaim, pk=kwargs["object_id"])

    def dispatch(self, request, *args, **kwargs):
        # Only the claim's own submitter (or a superuser, for support/admin
        # purposes) can add tickets to it - the button that links here is
        # already hidden for anyone else, this is defense in depth against
        # someone hitting the URL directly.
        if not request.user.is_superuser and self.claim.submitted_by_id != request.user.id:
            raise PermissionDenied
        if self.claim.status != ExpenseClaimStatus.DRAFT:
            messages.error(request, _("Tickets can only be added to a claim while it is still a draft."))
            return redirect(reverse("admin:expenses_expenseclaim_change", args=[self.claim.pk]))
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["claim"] = self.claim
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["claim"] = self.claim
        context["has_available_tickets"] = context["form"].fields["tickets"].queryset.exists()
        return context

    def form_valid(self, form):
        ticket_ids = list(form.cleaned_data["tickets"].values_list("id", flat=True))
        ExpenseTicket.objects.filter(id__in=ticket_ids).update(claim=self.claim)
        self.claim.recalculate_total()
        messages.success(self.request, _("%(n)d ticket(s) added to the claim.") % {"n": len(ticket_ids)})
        return redirect(reverse("admin:expenses_expenseclaim_change", args=[self.claim.pk]))
