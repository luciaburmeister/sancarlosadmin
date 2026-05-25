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

    def _get_preview_object(self, kind, product_id, company_id, object_id=None):
        from configurator.services.export.kind_registry import get_preview_object
        return get_preview_object(
            kind,
            product_id=product_id,
            company_id=company_id,
            object_id=object_id,
        )

    def post(self, request, *args, **kwargs):
        html_content = request.POST.get("html", "")
        css_content = request.POST.get("css", "")
        
        object_id = request.POST.get("object_id") or request.POST.get("config_id")
        template_kind = request.POST.get("kind", "quotation")
        product_id = request.POST.get("product")
        company_id = request.POST.get("company")
        
        target_obj = self._get_preview_object(template_kind, product_id, company_id, object_id)

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

        object_id = request.GET.get("object_id")
        target_obj = self._get_preview_object(template.kind, template.product_id, template.company_id, object_id)

        if not target_obj:
            return HttpResponse("No suitable object available for preview.", status=400)

        rendered_html = ConfigurationExportService.render_html(
            target_obj, template
        )

        return HttpResponse(rendered_html, content_type="text/html; charset=utf-8")

