from django.views.generic import FormView
from django.http import HttpResponse, Http404
from configurator.forms.configuration_export_form import ConfigurationExportForm
from configurator.services.export.service import ConfigurationExportService
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from assets.models import AssetTransaction
from unfold.views import UnfoldModelAdminViewMixin
from django.utils.translation import gettext_lazy as _


class AssetTransactionExportView(UnfoldModelAdminViewMixin, FormView):
    template_name = "configurator/export_form.html"
    form_class = ConfigurationExportForm
    permission_required = ("assets.view_assettransaction",)
    title = _("Asset Transaction Exporter")

    def dispatch(self, request, *args, **kwargs):
        self.transaction_id = kwargs.get("transaction_id")
        try:
            self.transaction = AssetTransaction.objects.select_related(
                "asset", "asset__asset_type", "asset__client",
                "from_zone", "to_zone", "company", "performed_by",
            ).get(id=self.transaction_id)
        except AssetTransaction.DoesNotExist:
            raise Http404(_("Asset Transaction not found"))

        self.available_templates = ConfigurationExportService.get_available_templates(
            self.transaction,
            request.user,
            ExportTemplateKind.ASSET_TRANSACTION,
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
            raise Http404(_("Template not available for this transaction"))

        rendered_html = ConfigurationExportService.render_html(
            self.transaction, template
        )

        return HttpResponse(
            rendered_html,
            content_type="text/html; charset=utf-8",
        )
