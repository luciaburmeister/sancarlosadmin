from django.views.generic.edit import FormView
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from unfold.views import UnfoldModelAdminViewMixin

from invoices.forms.invoice.bulk_mark_paid_form import BulkMarkPaidForm
from invoices.services.bulk_mark_paid_service import bulk_mark_invoices_paid


class BulkMarkPaidAdminView(UnfoldModelAdminViewMixin, FormView):
    form_class = BulkMarkPaidForm
    title = _("Bulk Mark Invoices as Paid")
    permission_required = ("invoices.change_salesinvoice",)
    template_name = "admin/invoices/bulk_mark_paid.html"

    def form_valid(self, form):
        company = form.cleaned_data["company"]
        raw_text = form.cleaned_data["invoice_numbers"]

        # Parse: one invoice number per line, strip whitespace, skip blanks
        invoice_numbers = [
            line.strip() for line in raw_text.splitlines() if line.strip()
        ]

        if not invoice_numbers:
            messages.warning(self.request, _("No invoice numbers provided."))
            return self.render_to_response(self.get_context_data(form=form))

        result = bulk_mark_invoices_paid(company, invoice_numbers)

        if result.marked > 0:
            messages.success(
                self.request,
                _("Marked %(count)d invoices as paid.") % {"count": result.marked},
            )
        if result.skipped > 0:
            messages.warning(
                self.request,
                _("%(count)d invoices were already paid (skipped).") % {"count": result.skipped},
            )
        if result.not_found > 0:
            messages.error(
                self.request,
                _("%(count)d invoice numbers were not found for this company.") % {"count": result.not_found},
            )

        return self.render_to_response(self.get_context_data(
            form=BulkMarkPaidForm(initial={"company": company}),
            bulk_result=result,
        ))
