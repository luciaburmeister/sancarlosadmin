"""
Admin view for confirming and creating a bulk invoice from multiple ProjectFiles.
"""
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile


class BulkInvoiceConfirmView(UnfoldModelAdminViewMixin, TemplateView):
    template_name = "invoices/bulk_invoice_confirm.html"
    permission_required = ("invoices.add_salesinvoice",)
    title = _("Bulk Invoice — Confirmation")

    def dispatch(self, request, *args, **kwargs):
        pf_ids = request.session.get("bulk_invoice_pf_ids", [])
        self.project_files = list(
            ProjectFile.objects.filter(pk__in=pf_ids)
            .select_related("client", "company")
        )
        if not self.project_files:
            messages.error(request, _("No project files selected."))
            return redirect(reverse("admin:management_projectfile_changelist"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        pf_details = []
        total_remaining = Decimal("0.00")
        total_items = 0

        for pf in self.project_files:
            summary = ProjectFileInvoiceService.get_invoiceable_summary(pf)
            remaining = summary["remaining"]
            items = BulkProjectFileInvoiceService._build_items_for_pf(pf, summary)
            item_count = len(items)
            pf_details.append({
                "reference": pf.reference,
                "name": pf.name,
                "remaining": remaining,
                "item_count": item_count,
            })
            total_remaining += remaining
            total_items += item_count

        ctx.update({
            "pf_details": pf_details,
            "pf_count": len(self.project_files),
            "client_name": self.project_files[0].client.name if self.project_files else "",
            "total_remaining": total_remaining,
            "total_items": total_items,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        if not request.POST.get("confirm"):
            return redirect(reverse("admin:management_projectfile_changelist"))

        pf = self.project_files[0]
        try:
            invoice = BulkProjectFileInvoiceService.create_bulk_invoice(
                project_files=self.project_files,
                company=pf.company,
                client=pf.client,
                date_issued=timezone.localdate(),
            )
        except ValidationError as e:
            messages.error(request, str(e.message))
            return redirect(reverse("admin:management_projectfile_changelist"))

        # Clear session
        request.session.pop("bulk_invoice_pf_ids", None)

        messages.success(
            request,
            _("Bulk invoice #%(id)s created successfully (%(total)s).") % {
                "id": invoice.id,
                "total": invoice.total_amount,
            },
        )
        return redirect(
            reverse("admin:invoices_salesinvoice_change", args=[invoice.pk])
        )
