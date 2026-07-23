from django.http import Http404, HttpResponse
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView
from unfold.views import UnfoldModelAdminViewMixin

from configurator.forms.configuration_export_form import ConfigurationExportForm
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export.service import ConfigurationExportService
from management.models import ProjectFile


class ProjectFileRepairOrderExportView(UnfoldModelAdminViewMixin, FormView):
    """Pick a repair-order template and render it for an expediente."""

    template_name = "configurator/export_form.html"
    form_class = ConfigurationExportForm
    permission_required = ("management.view_projectfile",)
    title = _("Repair Order")

    def dispatch(self, request, *args, **kwargs):
        try:
            self.project_file = ProjectFile.objects.select_related(
                "client", "company", "asset", "asset__asset_type"
            ).get(id=kwargs.get("project_file_id"))
        except ProjectFile.DoesNotExist:
            raise Http404(_("Project file not found"))

        self.available_templates = ConfigurationExportService.get_available_templates(
            self.project_file,
            request.user,
            ExportTemplateKind.REPAIR_ORDER,
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
            raise Http404(_("Template not available for this project file"))

        rendered_html = ConfigurationExportService.render_html(
            self.project_file, template
        )

        return HttpResponse(
            rendered_html,
            content_type="text/html; charset=utf-8",
        )
