from django.views.generic.edit import FormView
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from unfold.views import UnfoldModelAdminViewMixin

from invoices.forms.invoice.bulk_mark_invoice_paid_form import BulkMarkInvoicePaidForm
from invoices.services.bulk_update_invoice_payment_status import bulk_update_invoice_payment_status
from invoices.models import InvoicePaymentStatus


class BulkMarkInvoicePaidAdminView(UnfoldModelAdminViewMixin, FormView):
    form_class = BulkMarkInvoicePaidForm
    title = _("Bulk Update Payment Status")
    permission_required = ("invoices.bulk_mark_paid_invoice",)
    template_name = "admin/invoices/bulk_mark_invoice_paid.html"

    def form_valid(self, form):
        company = form.cleaned_data["company"]
        target_status = form.cleaned_data["action"]
        raw_text = form.cleaned_data["invoice_numbers"]

        # Parse: one invoice number per line, strip whitespace, skip blanks
        invoice_numbers = [
            line.strip() for line in raw_text.splitlines() if line.strip()
        ]

        if not invoice_numbers:
            messages.warning(self.request, _("No invoice numbers provided."))
            return self.render_to_response(self.get_context_data(form=form))

        result = bulk_update_invoice_payment_status(company, invoice_numbers, target_status)

        label = dict(InvoicePaymentStatus.choices).get(target_status, target_status)

        if result.updated > 0:
            messages.success(
                self.request,
                _("%(count)d invoices set to %(status)s.") % {"count": result.updated, "status": label},
            )
        if result.skipped > 0:
            messages.warning(
                self.request,
                _("%(count)d invoices already %(status)s (skipped).") % {"count": result.skipped, "status": label},
            )
        if result.ambiguous > 0:
            messages.error(
                self.request,
                _("%(count)d invoice numbers matched more than one provider and were skipped.") % {"count": result.ambiguous},
            )
        if result.not_found > 0:
            messages.error(
                self.request,
                _("%(count)d invoice numbers were not found for this company.") % {"count": result.not_found},
            )

        return self.render_to_response(self.get_context_data(
            form=BulkMarkInvoicePaidForm(initial={"company": company, "action": target_status}),
            bulk_result=result,
        ))