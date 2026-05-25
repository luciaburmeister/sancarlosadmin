from django.views.generic.edit import FormView
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from unfold.views import UnfoldModelAdminViewMixin

from invoices.forms.invoice.sales_invoice_import_form import SalesInvoiceImportForm
from invoices.services.import_sales_invoice_service import import_sales_invoices_from_excel


class SalesInvoiceImportAdminView(UnfoldModelAdminViewMixin, FormView):
    form_class = SalesInvoiceImportForm
    title = _("Import Sales Invoices from Excel")
    permission_required = ("invoices.add_salesinvoice",)
    template_name = "admin/invoices/sales_invoice_import.html"

    def form_valid(self, form):
        file_obj = form.cleaned_data["file"]
        default_year = form.cleaned_data.get("default_year")
        sheet_name = form.cleaned_data.get("sheet_name") or None

        result = import_sales_invoices_from_excel(
            file_obj=file_obj,
            default_year=default_year,
            sheet_name=sheet_name,
        )

        if result.created > 0:
            messages.success(
                self.request,
                _("Successfully imported %(count)d sales invoices.") % {"count": result.created},
            )
        if result.skipped > 0:
            messages.warning(
                self.request,
                _("Skipped %(count)d rows (duplicates or empty).") % {"count": result.skipped},
            )
        if result.errors > 0:
            messages.error(
                self.request,
                _("%(count)d rows had errors.") % {"count": result.errors},
            )

        # Re-render the form with results
        return self.render_to_response(self.get_context_data(
            form=SalesInvoiceImportForm(),
            import_result=result,
        ))
