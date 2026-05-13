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
        else:
            # Ticket already exists (re-conversion after going back to draft).
            # Sync ProductionItems with the current BOM: add new items, remove
            # obsolete ones that have no recorded progress, and refresh templates.
            cls._sync_items(ticket, configuration)
            cls._assign_default_templates(ticket, configuration)

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

    @classmethod
    def _create_items(cls, ticket, configuration):
        config_items = list(configuration.items.select_related("component"))
        cls._create_items_from_list(ticket, configuration, config_items)

    @staticmethod
    def _create_items_from_list(ticket, configuration, config_items):
        from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
        from configurator.services.export import ConfigurationExportService

        product = configuration.product
        ticket_template = ConfigurationExportTemplate.get_default_for_product(
            product=product,
            kind=ExportTemplateKind.PRODUCTION_TICKET,
            company=ticket.company,
        )

        for item in config_items:
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
                    "length": float(item.length) if item.length is not None else None,
                    "notes": item.note,
                },
                planning=planning,
                production_stages_order=list(item.production_stages or []),
            )
            if ticket_template:
                production_item.ticket_html = ConfigurationExportService.render_html(production_item, ticket_template)
                production_item.save(update_fields=["ticket_html"])

    @classmethod
    def _sync_items(cls, ticket, configuration):
        """Sync ProductionItems with the current BOM after a re-conversion.

        - Adds ProductionItems for new ConfigurationItems not yet in production.
        - Removes ProductionItems for ConfigurationItems that no longer exist in
          the BOM, but only when they carry no recorded progress (all stages
          pending, no sessions logged), so that ongoing work is never silently
          discarded.
        - Updates quantity/length/planning keys for existing items whose BOM
          data changed (preserving stage progress already recorded).
        """
        current_config_items = {
            item.id: item
            for item in configuration.items.select_related("component")
        }

        # Index existing ProductionItems by the ConfigurationItem ID they track
        existing_prod_items = {}
        for prod_item in ticket.items.all():
            cid = (prod_item.data or {}).get("configuration_item_id")
            if cid is not None:
                existing_prod_items[int(cid)] = prod_item

        existing_cids = set(existing_prod_items.keys())
        current_cids = set(current_config_items.keys())

        # 1. New config items → create ProductionItems
        new_config_items = [
            current_config_items[cid]
            for cid in current_cids - existing_cids
        ]
        if new_config_items:
            cls._create_items_from_list(ticket, configuration, new_config_items)

        # 2. Removed config items → delete ProductionItems that have no progress
        for cid in existing_cids - current_cids:
            prod_item = existing_prod_items[cid]
            has_progress = any(
                (info or {}).get("status") not in (None, "pending")
                for info in (prod_item.planning or {}).values()
            )
            has_sessions = prod_item.sessions.exists()
            if not has_progress and not has_sessions:
                prod_item.delete()

        # 3. Existing items still in BOM → update snapshot data and ensure
        #    planning keys are present for any new stages (preserving progress).
        for cid in existing_cids & current_cids:
            config_item = current_config_items[cid]
            prod_item = existing_prod_items[cid]
            changed = False

            new_data = {
                "configuration_item_id": config_item.id,
                "component_id": config_item.component_id,
                "component": str(config_item.component),
                "quantity": float(config_item.quantity),
                "length": float(config_item.length) if config_item.length is not None else None,
                "notes": config_item.note,
            }
            if prod_item.data != new_data:
                prod_item.data = new_data
                changed = True

            # Add planning entries for new stages; don't touch existing ones
            current_planning = prod_item.planning or {}
            new_stages = [
                s for s in (config_item.production_stages or [])
                if s not in current_planning
            ]
            if new_stages:
                for stage in new_stages:
                    current_planning[stage] = {
                        "status": "pending",
                        "started_at": None,
                        "completed_at": None,
                    }
                prod_item.planning = current_planning
                prod_item.production_stages_order = list(config_item.production_stages or [])
                changed = True

            if changed:
                prod_item.save(update_fields=["data", "planning", "production_stages_order"])
