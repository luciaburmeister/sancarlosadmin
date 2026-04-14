from django.views.generic import FormView
from django.http import HttpResponse, Http404
from configurator.forms.configuration_export_form import ConfigurationExportForm
from configurator.services.export.service import ConfigurationExportService
from configurator.models import ReceivalSlip, ConfigurationExportTemplate, ExportTemplateKind
from unfold.views import UnfoldModelAdminViewMixin
from django.utils.translation import gettext_lazy as _

class ReceivalSlipExportView(UnfoldModelAdminViewMixin, FormView):
    template_name = "configurator/export_form.html" 
    form_class = ConfigurationExportForm
    permission_required = ('configurator.view_receivalslip') 
    title = _("Receival Slip Exporter")

    def dispatch(self, request, *args, **kwargs):
        self.slip_id = kwargs.get("slip_id")
        try:
            self.receival_slip = ReceivalSlip.objects.get(id=self.slip_id)
        except ReceivalSlip.DoesNotExist:
            raise Http404(_("Receival Slip not found"))

        self.available_templates = ConfigurationExportService.get_available_templates(
            self.receival_slip,
            request.user,
            ExportTemplateKind.RECEIVAL_SLIP
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
            raise Http404(_("Template not available for this receival slip"))

        rendered_html = ConfigurationExportService.render_html(
            self.receival_slip, template
        )

        return HttpResponse(
            rendered_html,
            content_type="text/html; charset=utf-8"
        )
