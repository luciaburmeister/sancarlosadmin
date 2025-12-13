from django.views.generic import FormView
from django.http import HttpResponse, Http404
from configurator.forms import ConfigurationExportForm
from configurator.services.export_service import ConfigurationExportService
from configurator.models import Configuration, ConfigurationExportTemplate
from unfold.views import UnfoldModelAdminViewMixin


class ConfigurationExportView(UnfoldModelAdminViewMixin,FormView):
    template_name = "configurator/export_form.html" 
    form_class = ConfigurationExportForm
    permission_required = ('configurator.view_configurationexporttemplate') 
    title = "Configuration Template Exporter"

    def dispatch(self, request, *args, **kwargs):
        self.config_id = kwargs.get("config_id")
        try:
            self.configuration = Configuration.objects.get(id=self.config_id)
        except Configuration.DoesNotExist:
            raise Http404("Configuration not found")

        self.available_templates = ConfigurationExportService.get_available_templates(
            self.configuration
        )

        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["available_templates"] = self.available_templates
        return kwargs

    def form_valid(self, form):
        template_id = form.cleaned_data["template"]

        try:
            template = self.available_templates.get(id=template_id)
        except ConfigurationExportTemplate.DoesNotExist:
            raise Http404("Template not available for this configuration")

        html = ConfigurationExportService.render_html(
            self.configuration, template
        )

        rendered_html = ConfigurationExportService.render_html(
        self.configuration, template
    )

        return HttpResponse(
            rendered_html,
            content_type="text/html; charset=utf-8"
        )


