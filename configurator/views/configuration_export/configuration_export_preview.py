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

    def post(self, request, *args, **kwargs):
        html_content = request.POST.get("html", "")
        css_content = request.POST.get("css", "")
        
        config_id = request.POST.get("config_id")
        
        if config_id:
            configuration = Configuration.objects.get(id=config_id)
        else:
            configuration = Configuration.objects.first()
            
        if not configuration:
            return HttpResponse("No configuration available for preview.", status=400)

        template = ConfigurationExportTemplate(
            html=html_content,
            css=css_content
        )

        rendered_html = ConfigurationExportService.render_html(
            configuration, template
        )

        return HttpResponse(rendered_html, content_type="text/html; charset=utf-8")

    def get(self, request, *args, **kwargs):
        template_id = kwargs.get("template_id")
        try:
            template = ConfigurationExportTemplate.objects.get(id=template_id)
        except ConfigurationExportTemplate.DoesNotExist:
            raise Http404("Template not found")

        configuration = Configuration.objects.first()

        if not configuration:
            return HttpResponse("No configuration available for preview.", status=400)

        rendered_html = ConfigurationExportService.render_html(
            configuration, template
        )

        return HttpResponse(rendered_html, content_type="text/html; charset=utf-8")
