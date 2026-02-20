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
        from configurator.services.export_service import ConfigurationExportService
        
        product = configuration.product
        
        # 1. Production Order
        order_tpl = ConfigurationExportTemplate.get_default_for_product(
            product=product, 
            kind=ExportTemplateKind.PRODUCTION_ORDER
        )
        if order_tpl:
            ticket.order_html = ConfigurationExportService.render_html(configuration, order_tpl)
        
        # 2. Production Ticket (Default)
        ticket_tpl = ConfigurationExportTemplate.get_default_for_product(
            product=product, 
            kind=ExportTemplateKind.PRODUCTION_TICKET
        )
        if ticket_tpl:
            ticket.ticket_html = ConfigurationExportService.render_html(configuration, ticket_tpl)
        
        ticket.save(update_fields=["order_html", "ticket_html"])

    @staticmethod
    def _create_items(ticket, configuration):
        from configurator.services.export_service import ConfigurationExportService

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
            )

            # Assign ticket_html from ticket default
            prod_item.ticket_html = ticket.ticket_html
            prod_item.save(update_fields=["ticket_html"])
