from django.views.generic import FormView
from django.http import HttpResponse, Http404
from configurator.forms import ConfigurationExportForm
from configurator.services.export import ConfigurationExportService
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from sales.models import DeliveryOrder
from unfold.views import UnfoldModelAdminViewMixin
from django.utils.translation import gettext_lazy as _


class DeliveryOrderExportView(UnfoldModelAdminViewMixin, FormView):
    template_name = "configurator/export_form.html"
    form_class = ConfigurationExportForm
    permission_required = ()
    title = _("Delivery Order Template Exporter")

    def dispatch(self, request, *args, **kwargs):
        self.delivery_order_id = kwargs.get("delivery_order_id")
        try:
            self.delivery_order = DeliveryOrder.objects.get(id=self.delivery_order_id)
        except DeliveryOrder.DoesNotExist:
            raise Http404(_("Delivery Order not found"))

        self.available_templates = ConfigurationExportService.get_available_templates(
            self.delivery_order,
            request.user,
            ExportTemplateKind.DELIVERY_ORDER
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
            raise Http404(_("Template not available for this delivery order"))

        rendered_html = ConfigurationExportService.render_html(
            self.delivery_order, template
        )

        return HttpResponse(
            rendered_html,
            content_type="text/html; charset=utf-8"
        )
