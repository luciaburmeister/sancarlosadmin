from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.shortcuts import redirect
from unfold.views import UnfoldModelAdminViewMixin
from django.views import View
from invoices.models import Invoice, ConfirmationStatus


class InvoiceReviewAdminView(UnfoldModelAdminViewMixin, View):
    model_admin = None
    permission_required = ("invoices.change_invoice",)
    title = "Review Invoice"

    def get(self, request, object_id):
        invoice = get_object_or_404(Invoice, pk=object_id)

        context = {
            **self.model_admin.admin_site.each_context(request),
            "invoice": invoice,
            "items": invoice.items.all(),
            "taxes": invoice.get_taxes(),
            "title": f"Review Invoice: {invoice.invoice_number}",
        }
        return render(request, "admin/invoices/invoice/review.html", context)

    def post(self, request, object_id):
        invoice = get_object_or_404(Invoice, pk=object_id)
        new_status = request.POST.get("status")

        if new_status in ConfirmationStatus.values:
            invoice.confirmation_status = new_status
            invoice.save(update_fields=["confirmation_status"])
            self.model_admin.message_user(
                request,
                f"Invoice {invoice.invoice_number} marked as {invoice.get_confirmation_status_display()}."
            )

        return redirect(reverse("admin:invoices_invoice_changelist"))