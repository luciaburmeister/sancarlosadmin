from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from core.documents import enqueue_document
from purchasing.documents import render_purchase_order_pdf
from django.urls import reverse
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
    """Download a WeasyPrint PDF of a purchase order, rendered on the pdf queue."""

    model_admin = None

    def get(self, request, po_id):
        purchase_order = get_object_or_404(PurchaseOrder, pk=po_id)
        return enqueue_document(
            request,
            render_purchase_order_pdf,
            purchase_order_id=purchase_order.pk,
            filename=f"Pedido_Compra_{purchase_order.number}.pdf",
            back_url=reverse("admin:purchasing_purchaseorder_change", args=[purchase_order.pk]),
        )
