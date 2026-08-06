from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from djmoney.money import Money

from company.models import Company, CompanyScheduleSlot, Machine
from configurator.models import Configuration, ConfigurationItem, ProductTemplate
from materials.models import Component
from production.models import SchedulingRun
from production.tasks import run_schedule_optimization

# 2026-08-10 is a Monday
MONDAY = date(2026, 8, 10)


class RunScheduleOptimizationResyncTest(TestCase):
    STAGE = "corte"

    def setUp(self):
        self.company = Company.objects.create(
            name="Test Co",
            production_stages=[self.STAGE],
        )
        CompanyScheduleSlot.objects.create(
            company=self.company,
            weekday=0,  # Monday
            start_time=time(8, 0),
            end_time=time(10, 0),
        )
        self.product = ProductTemplate.objects.create(
            name="Ventana",
            company=self.company,
            production_stages=[self.STAGE],
        )
        self.component = Component.objects.create(
            company=self.company,
            name="Perfil",
            unit_price_expression="0",
        )
        # The post_save signal on Machine auto-creates the ProductionResource
        Machine.objects.create(
            company=self.company,
            name="Sierra",
            code="SIE-1",
            production_stages=[self.STAGE],
            daily_capacity_minutes=480,
            is_active=True,
        )
        self.configuration = Configuration.objects.create(
            company=self.company,
            product=self.product,
            name="Puerta",
            total_price=Money(1000, "EUR"),
            total_cost=Money(400, "EUR"),
        )
        # Stale snapshot: the item knows its stages but carries no resource
        # data, as happens when the template was configured after the
        # configuration was created.
        self.item = ConfigurationItem.objects.create(
            configuration=self.configuration,
            component=self.component,
            unit_price=Money(0, "EUR"),
            is_template_item=True,
            production_stages=[self.STAGE],
            stage_resources={self.STAGE: {}},
        )
        self.user = get_user_model().objects.create(username="planner")

    def test_run_resyncs_planning_snapshot_from_template(self):
        self.product.production_planning_resources = {
            self.STAGE: {
                "machine_types": [{"quantity": 1, "machine_type_id": 1}],
                "employee_types": [],
                "prep_time_min": 0,
                "work_time_min": 60,
                "post_time_min": 0,
            }
        }
        self.product.save(update_fields=["production_planning_resources"])

        run = SchedulingRun.objects.create(
            created_by=self.user,
            strategy="margin",
            start_date=MONDAY,
            end_date=MONDAY,
            slot_minutes=60,
            input_payload={"configuration_ids": [self.configuration.pk]},
        )

        run_schedule_optimization(run.pk)

        run.refresh_from_db()
        self.item.refresh_from_db()

        self.assertEqual(run.status, "solved")
        self.assertEqual(
            self.item.stage_resources[self.STAGE]["work_time_min"], 60
        )
        self.assertEqual(run.result_payload["metadata"]["resynced_items"], 1)
        # With the refreshed snapshot the configuration is plannable
        self.assertIn(
            self.configuration.pk,
            run.result_payload["selected_configuration_ids"],
        )

    def test_run_without_template_resources_reports_reason(self):
        # Template planning resources never configured: the resync keeps the
        # snapshot empty and the run explains why nothing was planned.
        run = SchedulingRun.objects.create(
            created_by=self.user,
            strategy="margin",
            start_date=MONDAY,
            end_date=MONDAY,
            slot_minutes=60,
            input_payload={"configuration_ids": [self.configuration.pk]},
        )

        run_schedule_optimization(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, "solved")
        reasons = run.result_payload["unselected_reasons"][str(self.configuration.pk)]
        self.assertEqual(reasons[0]["code"], "no_schedulable_stages")
