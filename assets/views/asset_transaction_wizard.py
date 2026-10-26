from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from unfold.views import UnfoldModelAdminViewMixin

from assets.forms import ManualTransactionForm, SplitReceiptForm
from assets.models import AssetPlanning, AssetTransactionReceipt, PlanningType
from assets.services.manual_transaction import create_manual_transaction


class AssetTransactionFromPlanningWizardView(UnfoldModelAdminViewMixin, FormView):
    """Guided "Create Transaction" form launched from an AssetPlanning's
    change page -- the manual-correction path for when the mobile app's
    automatic flow never ran. See assets.services.manual_transaction for
    what actually gets created and why it's not a literal reuse of the
    mobile strategies.
    """
    template_name = "assets/create_transaction_wizard.html"
    permission_required = ("assets.add_assettransaction",)
    title = _("Create Transaction")
    form_class = ManualTransactionForm

    SUPPORTED_PLANNING_TYPES = (PlanningType.CUBA, PlanningType.PORTE)

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.planning = get_object_or_404(AssetPlanning, pk=kwargs.get("planning_id"))

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["planning"] = self.planning
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["planning"] = self.planning
        context["unsupported_planning_type"] = self.planning.planning_type not in self.SUPPORTED_PLANNING_TYPES
        context["existing_transactions"] = self.planning.transactions.order_by("-date")[:10]
        return context

    def post(self, request, *args, **kwargs):
        # The template hides the form entirely for an unsupported planning
        # type; this guards a manually-crafted POST bypassing that.
        if self.planning.planning_type not in self.SUPPORTED_PLANNING_TYPES:
            messages.error(request, _("Manual transaction creation isn't supported yet for this planning type."))
            return redirect(reverse("admin:assets_assetplanning_change", args=[self.planning.pk]))
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            transaction = create_manual_transaction(
                planning=self.planning,
                transaction_type=form.cleaned_data["transaction_type"],
                zone=form.cleaned_data.get("zone"),
                performed_by=form.cleaned_data.get("performed_by"),
                notes=form.cleaned_data.get("notes", ""),
                data=form.build_data(),
                asset=form.cleaned_data.get("asset"),
                reference_number=form.cleaned_data.get("reference_number", ""),
            )
        except ValueError as e:
            messages.error(self.request, str(e))
            return self.form_invalid(form)

        messages.success(self.request, _("Transaction created."))
        return redirect(reverse("admin:assets_assettransaction_change", args=[transaction.pk]))


class ReceiptSplitWizardView(UnfoldModelAdminViewMixin, FormView):
    """"Split into multiple DIs" / "Add more DI parts" form, launched from
    the receipt's AssetTransaction change page (where plant/office staff
    actually look up a verteo) -- lets them turn one DI into several when a
    cuba arrives mixed with more than one residue, or grow an already-split
    load with more parts later (e.g. a part was missed the first time). See
    AssetTransactionReceipt.split_into_parts for what actually gets created.
    """
    template_name = "assets/split_receipt_wizard.html"
    permission_required = ("assets.add_assettransactionreceipt",)
    title = _("Split into Multiple DIs")
    form_class = SplitReceiptForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.receipt = get_object_or_404(AssetTransactionReceipt, pk=kwargs.get("receipt_id"))

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["current_total"] = self.receipt.total_parts
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["receipt"] = self.receipt
        context["already_split"] = self.receipt.total_parts > 1
        return context

    def _back_to_transaction(self):
        return redirect(reverse("admin:assets_assettransaction_change", args=[self.receipt.transaction_id]))

    def form_valid(self, form):
        try:
            self.receipt.split_into_parts(form.cleaned_data["total_parts"])
        except ValidationError as e:
            messages.error(self.request, str(e.message) if hasattr(e, "message") else str(e))
            return self.form_invalid(form)

        messages.success(self.request, _("DI split into %(n)d parts.") % {"n": form.cleaned_data["total_parts"]})
        return self._back_to_transaction()
