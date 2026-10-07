from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from apps.expenses.models import FixedCostInstallment, FixedCostInvoiceMatchSuggestion
from banking.services.transaction_matching import (
    AlreadyMatchedError,
    ConflictingReconciliationError,
    FixedCostInvoiceMatcher,
)


class FixedCostInvoiceMatchView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Review screen for suggested real invoices behind a recurring
    FixedCostInstallment. Candidates are mutually exclusive (an installment
    can only be matched to one invoice) - the user selects a single one via
    radio buttons and submits once; confirming it auto-rejects the siblings.
    """
    template_name = "admin/expenses/fixedcostinstallment/invoice_match_review.html"
    title = _("Suggested Matching Invoices")
    permission_required = ("expenses.change_fixedcostinstallment",)

    def get_installment(self):
        return get_object_or_404(FixedCostInstallment, pk=self.kwargs["object_id"])

    def get_pending_suggestions(self, installment):
        return (
            FixedCostInvoiceMatchSuggestion.objects
            .filter(installment=installment, status="pending")
            .select_related("invoice", "invoice__provider")
            .order_by("-confidence", "-created_at")
        )

    def get_confirmed_suggestion(self, installment):
        if not installment.matched_invoice_id:
            return None
        return (
            FixedCostInvoiceMatchSuggestion.objects
            .filter(installment=installment, invoice=installment.matched_invoice, status="confirmed")
            .select_related("reviewed_by")
            .order_by("-reviewed_at")
            .first()
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        installment = self.get_installment()
        context.update({
            "installment": installment,
            "suggestions": self.get_pending_suggestions(installment),
            "confirmed_suggestion": self.get_confirmed_suggestion(installment),
            "opts": FixedCostInstallment._meta,
        })
        return context

    def post(self, request, *args, **kwargs):
        installment = self.get_installment()
        action = request.POST.get("action")

        if action == "unmatch":
            FixedCostInvoiceMatcher.unmatch(installment, request.user)
            FixedCostInvoiceMatcher(installment).run()
            messages.info(request, _("Match removed. Pick a different invoice below."))
            return redirect(request.path)

        if action == "reject_all":
            for suggestion in self.get_pending_suggestions(installment):
                FixedCostInvoiceMatcher.reject(suggestion, request.user)
            messages.info(request, _("All suggestions rejected."))
            return redirect(request.path)

        if action == "confirm":
            suggestion_id = request.POST.get("selected_suggestion")
            if not suggestion_id:
                messages.error(request, _("Select an invoice first."))
                return redirect(request.path)

            suggestion = get_object_or_404(
                FixedCostInvoiceMatchSuggestion,
                pk=suggestion_id,
                installment=installment,
                status="pending",
            )
            acknowledge_conflict = request.POST.get("acknowledge_conflict") == "true"

            try:
                FixedCostInvoiceMatcher.confirm(
                    suggestion, request.user, acknowledge_conflict=acknowledge_conflict,
                )
            except AlreadyMatchedError as e:
                messages.error(
                    request,
                    _("This installment is already matched to \"%(invoice)s\" - unmatch it first.")
                    % {"invoice": e.current_invoice},
                )
                return redirect(request.path)
            except ConflictingReconciliationError:
                messages.warning(
                    request,
                    _(
                        "Both this installment and \"%(invoice)s\" already have their own, "
                        "different, bank reconciliations - linking them would imply one "
                        "settled the other, which doesn't match what the bank shows. "
                        "Review before confirming, and tick \"Confirm anyway\" if you're "
                        "sure this is still the right invoice."
                    ) % {"invoice": suggestion.invoice},
                )
                return redirect(request.path)

            siblings = FixedCostInvoiceMatchSuggestion.objects.filter(
                installment=installment, status="pending",
            ).exclude(pk=suggestion.pk)
            for sibling in siblings:
                FixedCostInvoiceMatcher.reject(sibling, request.user)

            messages.success(request, _("Match confirmed."))
            return redirect(request.path)

        messages.error(request, _("Unknown action."))
        return redirect(request.path)
