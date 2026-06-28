from django.shortcuts import redirect, get_object_or_404
from configurator.models import ProductTemplate, Configuration, ConfigurationStatus, ConfigurationItem
from unfold.views import UnfoldModelAdminViewMixin
from configurator.forms.shop_forms import ProjectFileSelectionForm, ProjectFileSelectionFormHelper
from configurator.forms import ConfigurationForm, ConfigurationFormHelper
from django.urls import reverse
from formtools.wizard.views import SessionWizardView
from django.utils.translation import gettext_lazy as _
from production.services.configuration_to_production import ConfigurationToProductionService
from configurator.services.receival_slip_service import ReceivalSlipService

class ShopConfigurationWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    form_list = [
        ("project_selection", ProjectFileSelectionForm),
        ("measures", ConfigurationForm),
    ]
    template_name = "configurator/wizard_form.html"
    permission_required = ('configurator.view_producttemplate') 
    title = _("Shop Configuration")

    def dispatch(self, request, product_id=None, config_id=None, *args, **kwargs):
        self.instance = None
        if config_id:
            from configurator.models import Configuration
            self.instance = get_object_or_404(Configuration, pk=config_id)
            self.product = self.instance.product
        else:
            self.product = get_object_or_404(ProductTemplate, pk=product_id)
            
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        kwargs["product"] = self.product
        kwargs["request"] = self.request
        if step == "measures":
            kwargs["instance"] = self.instance
        return kwargs

    def get_form_initial(self, step):
        initial = super().get_form_initial(step)
        if step == 'project_selection' and self.instance:
            from production.models import ProductionTicket
            from django.contrib.contenttypes.models import ContentType
            config_ct = ContentType.objects.get_for_model(self.instance)
            # Find the ticket linked to this configuration
            ticket = ProductionTicket.objects.filter(
                content_type=config_ct, 
                object_id=self.instance.id
            ).first()
            if ticket and ticket.project_file:
                initial['project_file'] = ticket.project_file
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["product"] = self.product
        
        if self.steps.current == "project_selection":
            context["helper"] = ProjectFileSelectionFormHelper()
        elif self.steps.current == "measures":
            context["helper"] = ConfigurationFormHelper(self.get_form(step=self.steps.current))

        return context

    def done(self, form_list, **kwargs):
        project_selection_form = form_list[0]
        config_form = form_list[1]
        
        project_file = project_selection_form.cleaned_data.get("project_file")
        client = project_file.client if project_file else None
        
        # 1. Update or Create Configuration
        config = config_form.save(client=client)

        # Link configuration directly to the expediente for cost passthrough
        if project_file and config.project_file_id != project_file.pk:
            config.project_file = project_file
            config.save(update_fields=['project_file'])
        
        # If we are NOT editing, do the automation stuff
        if not self.instance:
            config.status = ConfigurationStatus.SHOP
            config.save(update_fields=['status'])
            
            config.copy_default_pricing_rules_from_product()
            config.copy_default_taxes_from_product()
            config.copy_taxes_from_client()
            
            # 2. Automatically generate Production Ticket linked to ProjectFile
            ConfigurationToProductionService.create_ticket(config, project_file=project_file)

            # 3. Create Receival Slip (Empty items as requested)
            slip = ReceivalSlipService.create_from_configuration(config)
            
            url = reverse(
                "admin:configurator_receivalslip_change",
                args=[slip.id]
            )
        else:
            # If editing, just redirect back to config
            url = reverse(
                "admin:configurator_configuration_change",
                args=[config.id]
            )

        config.recalculate_price()

        # If editing and a production ticket exists, regenerate its HTML
        if self.instance:
            from django.contrib.contenttypes.models import ContentType
            from production.models import ProductionTicket

            config_ct = ContentType.objects.get_for_model(config)
            ticket = ProductionTicket.objects.filter(
                content_type=config_ct, object_id=config.id
            ).first()
            if ticket:
                ConfigurationToProductionService._assign_default_templates(ticket, config)

        return redirect(url)
