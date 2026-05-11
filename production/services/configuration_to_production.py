from django.contrib.contenttypes.models import ContentType
from configurator.models import Configuration, ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from production.models import ProductionTicket, ProductionItem
from production.services.session_scheduler import ScheduleContext, SessionScheduler
import os


class ConfigurationToProductionService:
    @classmethod
    def create_ticket_by_id(cls, configuration_id):
        configuration = Configuration.objects.get(pk=configuration_id)
        return cls.create_ticket(configuration)

    @classmethod
    def create_ticket(cls, configuration, project_file=None):
        ct = ContentType.objects.get_for_model(configuration)

        ticket, created = ProductionTicket.objects.get_or_create(
            content_type=ct,
            object_id=configuration.id,
            defaults={
                "company": configuration.company,
                "project_file": project_file,
                "context": {
                    "configuration_id": configuration.id,
                    "product": configuration.product.name,
                    "client": configuration.client_id,
                }
            },
        )

        if created:
            if configuration.work_order_pdf:
                ticket.work_order_pdf.save(
                    os.path.basename(configuration.work_order_pdf.name),
                    configuration.work_order_pdf,
                    save=False,
                )

            cls._assign_default_templates(ticket, configuration)
            cls._create_items(ticket, configuration)

        return ticket

    @classmethod
    def _assign_default_templates(cls, ticket, configuration):
        from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
        from configurator.services.export import ConfigurationExportService
        
        product = configuration.product
        
        # 1. Production Order
        order_template = ConfigurationExportTemplate.get_default_for_product(
            product=product, 
            kind=ExportTemplateKind.PRODUCTION_ORDER,
            company=ticket.company
        )
        if order_template:
            ticket.order_html = ConfigurationExportService.render_html(configuration, order_template)
        
        # 2. Production Ticket (Default)
        ticket_template = ConfigurationExportTemplate.get_default_for_product(
            product=product, 
            kind=ExportTemplateKind.PRODUCTION_TICKET,
            company=ticket.company
        )
        if ticket_template:
            ticket.ticket_html = ConfigurationExportService.render_html(configuration, ticket_template)
        
        ticket.save(update_fields=["order_html", "ticket_html", "work_order_pdf"])

    @staticmethod
    def _create_items(ticket, configuration):
        schedule_ctx = ScheduleContext.from_company(configuration.company)
        product = configuration.product

        for item in configuration.items.select_related("component"):
            planning = {
                stage: {
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                }
                for stage in item.production_stages
            }

            production_item = ProductionItem.objects.create(
                ticket=ticket,
                company=ticket.company,
                component=item.component,
                quantity=item.quantity,
                length=item.length,
                data={
                    "configuration_item_id": item.id,
                    "component_id": item.component_id,
                    "component": str(item.component),
                    "quantity": float(item.quantity),
                    "length": float(item.length),
                    "notes": item.note,
                },
                planning=planning,
                production_stages_order=list(item.production_stages or []),
            )

            ticket_template = ConfigurationExportTemplate.get_default_for_product(
                product=product,
                kind=ExportTemplateKind.PRODUCTION_TICKET,
                company=ticket.company
            )
            if ticket_template:
                production_item.ticket_html = ConfigurationExportService.render_html(production_item, ticket_template)

            production_item.save(update_fields=["ticket_html"])

            # Delegate scheduling to the dedicated SessionScheduler
            SessionScheduler.schedule_item(
                production_ticket=ticket,
                stage_resources=item.stage_resources,
                schedule_ctx=schedule_ctx,
            )
