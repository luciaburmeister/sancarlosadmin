from django.views.generic.edit import FormView
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.admin.helpers import AdminForm
from django.urls import reverse
from invoices.forms import InvoiceUploadForm
from unfold.views import UnfoldModelAdminViewMixin
from django.utils.translation import gettext_lazy as _
from invoices.services import invoice
from invoices.tasks import process_manual_invoice



class InvoiceUploadAdminView(UnfoldModelAdminViewMixin,FormView):
    form_class = InvoiceUploadForm
    title = _("Upload New Invoice")  
    permission_required = ("invoices.add_invoice",)  
    template_name = "admin/invoices/invoice_upload.html"


    def form_valid(self, form):
        form.save()
        invoice = form.instance
        process_manual_invoice.delay(invoice.id)
        messages.success(
        self.request, "Invoice uploaded. We're processing it with AI in the background.",
        )
        return redirect(reverse("admin:invoices_invoice_changelist"))
    