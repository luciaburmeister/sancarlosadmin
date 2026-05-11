from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.db.models import F, Sum, ExpressionWrapper, DurationField
from company.models import Employee, EmployeeType, Machine, MachineType
from production.models import ProductionResource, ProductionTicket, ProductionItem, PlannedProductionSession, PlannedProductionSessionSource
from datetime import datetime, time, timedelta
from configurator.models import Configuration, ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
import os
        
def get_or_create_resource_for_target(target) -> ProductionResource|None:
    if not target:
        return None
    
    ct = ContentType.objects.get_for_model(target)
    resource, _ = ProductionResource.objects.get_or_create(
        content_type=ct,
        object_id=target.pk,
        defaults={"is_active": True},
        company=target.company,
    )
    return resource

def get_resources(resource_type:MachineType|EmployeeType) -> list[ProductionResource]:
    resource_class = Machine if isinstance(resource_type, MachineType) else Employee

    original_resources = list(
        resource_class.objects
        .filter(type=resource_type)
    ) 

    resources = []

    for res in original_resources:
        resource = get_or_create_resource_for_target(res)
        resources.append(resource)

    return resources
    
def possible_resources_for_stage(resources:list[ProductionResource], day, duration: timedelta) -> set:
    start_of_day = timezone.make_aware(datetime.combine(day, time.min))
    end_of_day = timezone.make_aware(datetime.combine(day, time.max))

    time_used_by_resource = dict(
        PlannedProductionSession.objects
        .filter(
            primary_resource__in=resources,
            planned_start__lt=end_of_day,
            planned_end__gt=start_of_day,
        )
        .annotate(
            duration=ExpressionWrapper(
                F("planned_end") - F("planned_start"),
                output_field=DurationField(),
            )
        )
        .values("primary_resource")
        .annotate(total=Sum("duration"))
        .values_list("primary_resource", "total")
    )

    possible_resources = set()
    for res in resources:
        used_time = time_used_by_resource.get(res.pk) or timedelta(0)
        machine = Machine.objects.filter(pk=res.object_id).first()
        capacity = timedelta(minutes=getattr(machine, "daily_capacity_minutes", 0) or 0)
        if used_time + duration <= capacity:
            possible_resources.add(res.pk)
    
    return possible_resources

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
        slots = configuration.company.schedule_slots.all().order_by("weekday", "start_time")

        if not slots.exists():
            raise ValueError("Company has no schedule slots configured")

        # Simplification: assume same schedule every day → take first slot
        max_start = max(slot.start_time for slot in slots)
        min_end = min(slot.end_time for slot in slots)

        SCHEDULE = {
            "start": max_start,
            "end": min_end,
        }
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

            last_stage_session = None

            for stage, stage_info in (item.stage_resources or {}).items():
                if not last_stage_session:
                    tomorrow = timezone.localdate() + timedelta(days=1)
                    search_start_time = timezone.make_aware(datetime.combine(tomorrow, SCHEDULE["start"]))
                else:
                    search_start_time = last_stage_session.planned_end

                duration = timedelta(minutes=int(stage_info["total_time_min"]))

                machines = stage_info.get("machine_types", [])
                employees = stage_info.get("employee_types", [])

                resource_type = MachineType.objects.filter(pk=machines[0]["machine_type_id"]).first() if machines else EmployeeType.objects.filter(pk=employees[0]["employee_type_id"]).first()
                if not resource_type:
                    continue
                resources = get_resources(resource_type)                    

                max_days_ahead = 30
                days_ahead = 0

                while True:
                    end_of_day = timezone.make_aware(
                        timezone.datetime.combine(search_start_time.date(), SCHEDULE["end"])
                    )
                    
                    if isinstance(resource_type, MachineType):
                        possible_resources = possible_resources_for_stage(resources, search_start_time.date(), duration)
                    else:
                        possible_resources = set(res.pk for res in resources)

                    if search_start_time + duration > end_of_day or not possible_resources:
                        days_ahead += 1
                        if days_ahead > max_days_ahead:
                            break
                        search_start_time = timezone.make_aware(
                            timezone.datetime.combine(
                                search_start_time.date() + timedelta(days=1),
                                SCHEDULE["start"]
                            )
                        )
                        continue

                    conflict_slots = PlannedProductionSession.objects.filter(planned_start__lt=search_start_time + duration, planned_end__gt=search_start_time, primary_resource__in=resources).order_by("-planned_end")
                    used_resources = set(conflict_slots.values_list("primary_resource_id", flat=True).distinct())

                    available_resources = possible_resources - used_resources

                    if not available_resources:
                        search_start_time = conflict_slots.first().planned_end
                        continue
                        
                    last_stage_session = PlannedProductionSession.objects.create(
                        production_item = production_item,
                        production_stage = stage,
                        planned_start = search_start_time,
                        planned_end = search_start_time + duration,
                        locked = False,
                        source = PlannedProductionSessionSource.AUTOMATIC,
                        primary_resource_id =  next(iter(available_resources)),
                    )
                    break                            
