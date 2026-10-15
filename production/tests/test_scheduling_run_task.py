from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from production.models import SchedulingRun
from production.tasks import run_schedule_optimization
from production.tests.factories import make_machine, make_priced_configuration, make_scheduling_world

# 2026-08-10 is a Monday
MONDAY = date(2026, 8, 10)


class RunScheduleOptimizationResyncTest(TestCase):
    STAGE = "corte"

    def setUp(self):
        self.company, self.product, self.component = make_scheduling_world(stage=self.STAGE)
        # The post_save signal on Machine auto-creates the ProductionResource
        make_machine(
            self.company, name="Sierra", code="SIE-1", stages=[self.STAGE],
            daily_capacity_minutes=480, is_active=True,
        )
        # Stale snapshot: the item knows its stages but carries no resource
        # data, as happens when the template was configured after the
        # configuration was created.
        self.configuration = make_priced_configuration(
            product=self.product, component=self.component, name="Puerta",
            price=1000, cost=400, stages=[self.STAGE], stage_resources={self.STAGE: {}},
        )
        self.item = self.configuration.items.get()
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
