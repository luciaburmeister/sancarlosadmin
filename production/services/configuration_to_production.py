from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.db.models import F, Sum, ExpressionWrapper, DurationField
from company.models import Machine, MachineType
from production.models import ProductionResource, ProductionTicket, ProductionItem, PlannedProductionSession, PlannedProductionSessionSource
from datetime import datetime, time, timedelta

def get_or_create_resource_for_target(target):
    if not target:
        return None
    
    ct = ContentType.objects.get_for_model(target)
    resource, _ = ProductionResource.objects.get_or_create(
        content_type=ct,
        object_id=target.pk,
        defaults={"is_active": True},
    )
    return resource
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
            },
        )

        if created:
            cls._create_items(ticket, configuration)

        return ticket

    @staticmethod
    def _create_items(ticket, configuration):
        SCHEDULE = {"start": 9, "end": 17}

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

            for stage, stage_info in (item.stage_resources or {}).items():
                tomorrow = timezone.localdate() + timedelta(days=1)
                search_start_time = timezone.make_aware(datetime.combine(tomorrow, time(hour=SCHEDULE["start"])))
                duration = timedelta(minutes=int(stage_info["total_time_min"]))

                machines = stage_info.get("machine_types", [])
                machine_type = MachineType.objects.filter(pk=machines[0]["machine_type_id"]).first() if machines else None
                primary = get_or_create_resource_for_target(machine_type)

                num_machines = len(Machine.objects.filter(type=machine_type))

                total_possible_time = Machine.objects.filter(type=machine_type).aggregate(total=Sum("daily_capacity_minutes"))["total"] or 0

                while True:
                    start_of_day = timezone.make_aware(
                        timezone.datetime.combine(search_start_time.date(), time(hour=SCHEDULE["start"]))
                    )
                    end_of_day = timezone.make_aware(
                        timezone.datetime.combine(search_start_time.date(), time(hour=SCHEDULE["end"]))
                    )

                    used_duration = (
                        PlannedProductionSession.objects
                        .filter(
                            planned_start__gte=start_of_day,
                            planned_end__lte=end_of_day,
                            primary_resource=primary,
                        )
                        .annotate(
                            duration=ExpressionWrapper(
                                F("planned_end") - F("planned_start"),
                                output_field=DurationField(),
                            )
                        )
                        .aggregate(total=Sum("duration"))
                    )["total"] or timedelta(0)

                    used_minutes = used_duration.total_seconds() / 60
                    duration_minutes = duration.total_seconds() / 60

                    if search_start_time + duration > end_of_day or used_minutes + duration_minutes > total_possible_time:
                        search_start_time = timezone.make_aware(
                            timezone.datetime.combine(
                                search_start_time.date() + timedelta(days=1),
                                time(hour=SCHEDULE["start"])
                            )
                        )
                        continue

                    next_busy_slots = PlannedProductionSession.objects.filter(planned_start__gte=search_start_time, planned_start__lt=search_start_time + duration, primary_resource=primary).order_by("-planned_end")

                    if len(next_busy_slots) >= num_machines:
                        search_start_time = next_busy_slots.first().planned_end
                    else:
                        PlannedProductionSession.objects.create(
                            production_item = production_item,
                            production_stage = stage,
                            planned_start = search_start_time,
                            planned_end = search_start_time + timedelta(minutes=stage_info["total_time_min"]),
                            locked = False,
                            source = PlannedProductionSessionSource.AUTOMATIC,
                            primary_resource = primary,
                        )
                        break
