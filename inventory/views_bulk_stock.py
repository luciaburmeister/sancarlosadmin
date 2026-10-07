from django.contrib import messages
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from inventory.forms import BulkStockUpdateForm
from inventory.models import InventoryItem
from inventory.services.bulk_stock import apply_bulk_stock


class BulkStockUpdateView(UnfoldModelAdminViewMixin, FormView):
    """Paste counted stock from a spreadsheet, preview the differences, apply.

    Submitting the rows validates them and shows what would change; only the
    confirmation (``apply``) writes, re-reading balances under lock and
    recording every adjustment in one transaction. ``edit`` goes back to the
    textarea without losing the paste.
    """

    title = _("Bulk stock update")
    permission_required = ("inventory.change_inventoryitem",)
    template_name = "admin/inventory/bulk_stock_update.html"
    form_class = BulkStockUpdateForm
    success_url = reverse_lazy("admin:inventory_inventoryitem_changelist")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["companies"] = get_user_companies(self.request.user)
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["opts"] = InventoryItem._meta
        return context

    def form_valid(self, form):
        if "apply" not in self.request.POST:
            confirm = "edit" not in self.request.POST
            if confirm:
                # What gets applied is what was previewed: lock the paste.
                form.fields["rows"].widget.attrs["readonly"] = True
            return self.render_to_response(self.get_context_data(form=form, confirm=confirm))

        plan = apply_bulk_stock(
            form.cleaned_data["rows"],
            form.companies,
            user=self.request.user,
            note=form.cleaned_data["note"],
        )
        if not plan.is_valid:
            form.plan = plan
            form.add_error("rows", _("Nothing was applied: fix the rows marked below."))
            return self.form_invalid(form)

        messages.success(
            self.request,
            _("Stock updated: %(changed)d adjustment(s) recorded, %(same)d row(s) already matched.")
            % {"changed": len(plan.changes), "same": len(plan.rows) - len(plan.changes)},
        )
        return super().form_valid(form)
