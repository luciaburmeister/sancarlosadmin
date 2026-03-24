from django.views import View
from django.http import HttpResponse, Http404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_exempt
from django.contrib.auth.mixins import PermissionRequiredMixin
from configurator.models import Configuration, ConfigurationExportTemplate
from configurator.services.export import ConfigurationExportService
import json
from unfold.views import UnfoldModelAdminViewMixin


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(xframe_options_exempt, name='dispatch')
class ExportTemplatePreviewView(UnfoldModelAdminViewMixin, View):
    permission_required = "configurator.view_configurationexporttemplate"
    """
    Renders a preview of an export template.
    Can be used by passing template_id or by posting html and css content.
    """

    def _get_preview_object(self, kind, product_id, company_id, config_id=None):
        filters = {}
        if company_id:
            filters["company_id"] = company_id
            
        if kind == "quotation":
            from configurator.models import Configuration
            if config_id:
                return Configuration.objects.filter(id=config_id).first()
            
            qs = Configuration.objects.filter(**filters)
            if product_id:
                qs = qs.filter(product_id=product_id)
            return qs.first() or Configuration.objects.first()
            
        elif kind in ["production_order", "production_ticket"]:
            from production.models import ProductionItem
            qs = ProductionItem.objects.all()
            if company_id:
                qs = qs.filter(company_id=company_id)
            if product_id:
                for item in qs[:50]:
                    product = getattr(item.ticket.source_object, "product", None) if getattr(item, 'ticket', None) else None
                    if product and str(product.id) == str(product_id):
                        return item
            return qs.first()
            
        elif kind == "sales_invoice":
            from invoices.models import SalesInvoice
            qs = SalesInvoice.objects.filter(**filters)
            if product_id:
                qs = qs.filter(configuration__product_id=product_id)
            return qs.first() or SalesInvoice.objects.first()
            
        return None

    def post(self, request, *args, **kwargs):
        html_content = request.POST.get("html", "")
        css_content = request.POST.get("css", "")
        
        config_id = request.POST.get("config_id")
        template_kind = request.POST.get("kind", "quotation")
        product_id = request.POST.get("product")
        company_id = request.POST.get("company")
        
        target_obj = self._get_preview_object(template_kind, product_id, company_id, config_id)

        if not target_obj:
            return HttpResponse("No suitable object available for preview.", status=400)

        template = ConfigurationExportTemplate(
            html=html_content,
            css=css_content,
            kind=template_kind
        )

        rendered_html = ConfigurationExportService.render_html(
            target_obj, template
        )

        return HttpResponse(rendered_html, content_type="text/html; charset=utf-8")

    def get(self, request, *args, **kwargs):
        template_id = kwargs.get("template_id")
        try:
            template = ConfigurationExportTemplate.objects.get(id=template_id)
        except ConfigurationExportTemplate.DoesNotExist:
            raise Http404("Template not found")

        target_obj = self._get_preview_object(template.kind, template.product_id, template.company_id, None)

        if not target_obj:
            return HttpResponse("No suitable object available for preview.", status=400)

        rendered_html = ConfigurationExportService.render_html(
            target_obj, template
        )

        return HttpResponse(rendered_html, content_type="text/html; charset=utf-8")

