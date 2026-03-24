from django.contrib.contenttypes.models import ContentType
from production.models import ProductionTicket, ProductionItem

class ConfigurationToProductionService:

    @classmethod
    def create_ticket(cls, configuration):
        ct = ContentType.objects.get_for_model(configuration)

        ticket, created = ProductionTicket.objects.get_or_create(
            content_type=ct,
            object_id=configuration.id,
            defaults={
                "company": configuration.company,
                "context": {
                    "configuration_id": configuration.id,
                    "product": configuration.product.name,
                    "client": configuration.client_id,
                }
            }
        )

        if created:
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
        
        ticket.save(update_fields=["order_html", "ticket_html"])

    @staticmethod
    def _create_items(ticket, configuration):
        from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
        from configurator.services.export import ConfigurationExportService

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

            prod_item = ProductionItem.objects.create(
                ticket=ticket,
                company=ticket.company,
                component=item.component,
                quantity=item.quantity,
                length=item.length,
                data={
                    "component_id": item.component_id,
                    "component": str(item.component),
                    "quantity": float(item.quantity),
                    "length": float(item.length),
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
                prod_item.ticket_html = ConfigurationExportService.render_html(prod_item, ticket_template)
            
            prod_item.save(update_fields=["ticket_html"])
