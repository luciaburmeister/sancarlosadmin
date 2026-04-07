from django.views.generic import FormView
from django.http import HttpResponse, Http404
from configurator.forms import ConfigurationExportForm
from configurator.services.export import ConfigurationExportService
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from sales.models import Quote
from unfold.views import UnfoldModelAdminViewMixin
from django.utils.translation import gettext_lazy as _

class QuoteExportView(UnfoldModelAdminViewMixin, FormView):
    template_name = "configurator/export_form.html" 
    form_class = ConfigurationExportForm
    permission_required = ('configurator.view_configurationexporttemplate') 
    title = _("Quote Template Exporter")

    def dispatch(self, request, *args, **kwargs):
        self.quote_id = kwargs.get("quote_id")
        try:
            self.quote = Quote.objects.get(id=self.quote_id)
        except Quote.DoesNotExist:
            raise Http404(_("Quote not found"))

        self.available_templates = ConfigurationExportService.get_available_templates(
            self.quote,
            request.user,
            ExportTemplateKind.CONSOLIDATED_QUOTATION
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
            raise Http404(_("Template not available for this quote"))

        rendered_html = ConfigurationExportService.render_html(
            self.quote, template
        )

        return HttpResponse(
            rendered_html,
            content_type="text/html; charset=utf-8"
        )
