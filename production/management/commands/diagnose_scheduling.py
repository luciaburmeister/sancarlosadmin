from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from configurator.models import Configuration
from production.services.scheduler.data import optimization_dict_creation
from production.services.scheduler.reasons import describe_reason
from production.services.scheduler.strategies.margin import MarginScheduler


class Command(BaseCommand):
    help = (
        "Explains why a configuration cannot get a planning: runs the "
        "scheduler's structural checks (stages, resources, time slots, "
        "horizon) for the given configurations without solving."
    )

    def add_arguments(self, parser):
        parser.add_argument("configuration_ids", nargs="+", type=int)
        parser.add_argument(
            "--start", type=date.fromisoformat, default=None,
            help="Start of the date range (YYYY-MM-DD, default: today)",
        )
        parser.add_argument(
            "--end", type=date.fromisoformat, default=None,
            help="End of the date range (YYYY-MM-DD, default: start + 14 days)",
        )
        parser.add_argument(
            "--ts", type=int, default=10,
            help="Slot length in minutes (default: 10, as in scheduling runs)",
        )

    def handle(self, *args, **options):
        config_ids = options["configuration_ids"]
        start_date = options["start"] or date.today()
        end_date = options["end"] or start_date + timedelta(days=14)
        ts = options["ts"]

        configurations = list(Configuration.objects.filter(pk__in=config_ids))
        missing = set(config_ids) - {c.pk for c in configurations}
        if missing:
            raise CommandError(f"Configuration(s) not found: {sorted(missing)}")

        scheduler = MarginScheduler(optimization_params={"ts": ts})

        try:
            (
                company_ordered_slots, configuration_company,
                configuration_margin, configuration_stages,
                stage_resource_types, stage_resource_type_quantity,
                stage_duration, resource_type_active_resources,
                resource_parallel_capacity, _daily_capacity,
                _slot_load, _day_load,
            ) = optimization_dict_creation(start_date, end_date, configurations, ts)
        except ValidationError as exc:
            raise CommandError(
                "Could not build the optimization data — a stage resource "
                f"expression fails to evaluate: {exc.message_dict}"
            )

        stage_required_slots = scheduler._stage_required_slots(stage_duration, ts)
        stage_suffix_slots = scheduler._config_stage_suffix_slots(
            configuration_stages, stage_required_slots,
        )
        stage_prefix_slots = scheduler._config_stage_prefix_slots(
            configuration_stages, stage_required_slots,
        )
        valid_start_positions = scheduler._valid_stage_start_positions(
            company_ordered_slots, configuration_company, configurations,
            configuration_stages, stage_suffix_slots, stage_prefix_slots,
        )
        structural_reasons = scheduler._structural_unschedulable_reasons(
            configurations, configuration_company, company_ordered_slots,
            configuration_stages, stage_resource_types,
            stage_resource_type_quantity, stage_suffix_slots,
            valid_start_positions, resource_type_active_resources,
            resource_parallel_capacity,
        )

        self.stdout.write(
            f"Date range {start_date} .. {end_date}, slot = {ts} min\n"
        )

        for c in configurations:
            margin = configuration_margin[c]
            stages = configuration_stages[c]
            company = configuration_company[c]
            slots = len(company_ordered_slots.get(company, []))

            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{c}"))
            self.stdout.write(f"  Company: {company} ({slots} slots in range)")
            self.stdout.write(f"  Margin: {margin:.2f}")
            self.stdout.write(f"  Schedulable stages: {stages or '—'}")
            for s in stages:
                needed = stage_required_slots[(c, s)]
                rts = [
                    f"{rt[0].model} x{stage_resource_type_quantity[(c, s, rt)]}"
                    f" ({len(resource_type_active_resources.get(rt, []))} active)"
                    for rt in stage_resource_types[(c, s)]
                ]
                self.stdout.write(
                    f"    - {s}: {stage_duration[(c, s)]} min = {needed} slot(s); "
                    f"resources: {', '.join(rts) or 'none'}"
                )

            reasons_list = structural_reasons.get(c.pk, [])
            strategy_reason = scheduler._strategy_unselected_reason(c, margin)
            if not reasons_list and strategy_reason:
                reasons_list = [strategy_reason]

            if reasons_list:
                self.stdout.write(self.style.ERROR("  Cannot be planned:"))
                for reason in reasons_list:
                    self.stdout.write(f"    * {describe_reason(reason)}")
            else:
                self.stdout.write(self.style.SUCCESS(
                    "  Structurally schedulable. If it was not planned in a "
                    "run, it lost slots/resources to configurations the "
                    "optimizer preferred — check the run preview's "
                    "\"Configurations without planning\" table."
                ))
