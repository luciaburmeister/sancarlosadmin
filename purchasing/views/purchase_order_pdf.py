from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from core.services.pdf_service import PDFService
from ..models import PurchaseOrder


class PurchaseOrderPdfView(View):
    """Browser preview: rendered HTML of the purchase order export template."""

    model_admin = None

    def get(self, request, po_id):
        purchase_order = get_object_or_404(PurchaseOrder, pk=po_id)
        template = ConfigurationExportTemplate.get_default_for_product(
            product=None,
            kind=ExportTemplateKind.PURCHASE_ORDER,
            company=purchase_order.company,
        )
        if not template:
            return HttpResponse("No purchase order template found.", status=404)

        html = ConfigurationExportService.render_html(purchase_order, template)
        return HttpResponse(html)


class PurchaseOrderPdfDownloadView(View):
    """Force-download a WeasyPrint PDF of a purchase order."""

    model_admin = None

    def get(self, request, po_id):
        purchase_order = get_object_or_404(
            PurchaseOrder.objects.select_related("company", "provider"), pk=po_id
        )
        pdf_bytes = PDFService.render_object_pdf(
            purchase_order,
            ExportTemplateKind.PURCHASE_ORDER,
            company=purchase_order.company,
        )
        if not pdf_bytes:
            return HttpResponse("No purchase order template found.", status=404)

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="Pedido_Compra_{purchase_order.number}.pdf"'
        return response
