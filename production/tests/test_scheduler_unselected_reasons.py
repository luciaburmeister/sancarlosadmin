from datetime import date, time

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from djmoney.money import Money

from company.models import Company, CompanyScheduleSlot, Machine
from configurator.models import Configuration, ConfigurationItem, ProductTemplate
from materials.models import Component
from production.models import ProductionResource
from production.services.scheduler import reasons
from production.services.scheduler.reasons import describe_reason
from production.services.scheduler.serialization import (
    proposal_from_dict,
    proposal_to_dict,
)
from production.services.scheduler.strategies.margin import MarginScheduler

# 2026-08-10 is a Monday
MONDAY = date(2026, 8, 10)


class SchedulerUnselectedReasonsTest(TestCase):
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

    def _create_configuration(self, *, name, price, cost, stages=None, work_time_min=60):
        configuration = Configuration.objects.create(
            company=self.company,
            product=self.product,
            name=name,
            total_price=Money(price, "EUR"),
            total_cost=Money(cost, "EUR"),
        )
        stages = [self.STAGE] if stages is None else stages
        stage_resources = {
            stage: {
                "machine_types": [{"quantity": 1, "machine_type_id": 1}],
                "employee_types": [],
                "prep_time_min": 0,
                "work_time_min": work_time_min,
                "post_time_min": 0,
            }
            for stage in stages
        }
        ConfigurationItem.objects.create(
            configuration=configuration,
            component=self.component,
            unit_price=Money(0, "EUR"),
            is_template_item=True,
            production_stages=stages,
            stage_resources=stage_resources,
        )
        return configuration

    def _create_machine_resource(self, *, parallel_capacity=1):
        # The post_save signal on Machine auto-creates the ProductionResource
        machine = Machine.objects.create(
            company=self.company,
            name="Sierra",
            code="SIE-1",
            production_stages=[self.STAGE],
            daily_capacity_minutes=480,
            parallel_capacity=parallel_capacity,
            is_active=True,
        )
        return ProductionResource.objects.get(
            content_type=ContentType.objects.get_for_model(Machine),
            object_id=machine.id,
        )

    def _schedule(self, configurations, *, ts=60, start=MONDAY, end=MONDAY):
        scheduler = MarginScheduler(optimization_params={"ts": ts})
        return scheduler.schedule(
            start_date=start, end_date=end, configurations=configurations
        )

    def test_selected_configuration_has_no_reason(self):
        self._create_machine_resource()
        configuration = self._create_configuration(name="OK", price=1000, cost=400)

        proposal = self._schedule([configuration])

        self.assertIn(configuration.pk, proposal.selected_configuration_ids)
        self.assertNotIn(configuration.pk, proposal.unselected_reasons)

    def test_no_active_resource_reason(self):
        # No machine resource created at all
        configuration = self._create_configuration(name="Sin maquina", price=1000, cost=400)

        proposal = self._schedule([configuration])

        self.assertIn(configuration.pk, proposal.unselected_configuration_ids)
        codes = [r["code"] for r in proposal.unselected_reasons[configuration.pk]]
        self.assertIn(reasons.NO_ACTIVE_RESOURCES, codes)

    def test_no_schedulable_stages_reason(self):
        self._create_machine_resource()
        configuration = self._create_configuration(
            name="Sin etapas", price=1000, cost=400, stages=[]
        )

        proposal = self._schedule([configuration])

        self.assertIn(configuration.pk, proposal.unselected_configuration_ids)
        codes = [r["code"] for r in proposal.unselected_reasons[configuration.pk]]
        self.assertIn(reasons.NO_SCHEDULABLE_STAGES, codes)

    def test_horizon_too_short_reason(self):
        self._create_machine_resource()
        # 2 slots available (08:00-10:00, ts=60) but 3 needed
        configuration = self._create_configuration(
            name="No cabe", price=1000, cost=400, work_time_min=180
        )

        proposal = self._schedule([configuration])

        self.assertIn(configuration.pk, proposal.unselected_configuration_ids)
        reason_list = proposal.unselected_reasons[configuration.pk]
        codes = [r["code"] for r in reason_list]
        self.assertIn(reasons.HORIZON_TOO_SHORT, codes)
        horizon_reason = next(
            r for r in reason_list if r["code"] == reasons.HORIZON_TOO_SHORT
        )
        self.assertEqual(horizon_reason["needed_slots"], 3)
        self.assertEqual(horizon_reason["available_slots"], 2)

    def test_non_positive_margin_reason(self):
        self._create_machine_resource()
        configuration = self._create_configuration(
            name="Sin margen", price=100, cost=200
        )

        proposal = self._schedule([configuration])

        self.assertIn(configuration.pk, proposal.unselected_configuration_ids)
        codes = [r["code"] for r in proposal.unselected_reasons[configuration.pk]]
        self.assertIn(reasons.NON_POSITIVE_MARGIN, codes)

    def test_capacity_competition_reason(self):
        # One machine with a single slot's worth of capacity per Monday and
        # two configurations that each need both available slots: only the
        # higher-margin one fits.
        self._create_machine_resource()
        winner = self._create_configuration(
            name="Gana", price=2000, cost=400, work_time_min=120
        )
        loser = self._create_configuration(
            name="Pierde", price=1000, cost=400, work_time_min=120
        )

        proposal = self._schedule([winner, loser])

        self.assertIn(winner.pk, proposal.selected_configuration_ids)
        self.assertIn(loser.pk, proposal.unselected_configuration_ids)
        codes = [r["code"] for r in proposal.unselected_reasons[loser.pk]]
        self.assertIn(reasons.CAPACITY_COMPETITION, codes)

    def test_reasons_survive_serialization_round_trip(self):
        configuration = self._create_configuration(name="Sin maquina", price=1000, cost=400)

        proposal = self._schedule([configuration])
        restored = proposal_from_dict(proposal_to_dict(proposal))

        self.assertEqual(restored.unselected_reasons, proposal.unselected_reasons)
        self.assertIn(configuration.pk, restored.unselected_reasons)

    def test_describe_reason_renders_every_code(self):
        reason_dicts = [
            {"code": reasons.NO_SCHEDULABLE_STAGES},
            {"code": reasons.NO_COMPANY_TIME_SLOTS, "company": "Test Co"},
            {
                "code": reasons.HORIZON_TOO_SHORT,
                "needed_slots": 3,
                "needed_minutes": 180,
                "available_slots": 2,
            },
            {"code": reasons.NO_ACTIVE_RESOURCES, "stage": "corte", "kind": "machine"},
            {
                "code": reasons.INSUFFICIENT_RESOURCE_CAPACITY,
                "stage": "corte",
                "kind": "employee",
                "required": 2,
                "available": 1,
            },
            {"code": reasons.NON_POSITIVE_MARGIN, "margin": -100.0},
            {"code": reasons.SOLVER_INCOMPLETE, "status": "Not Solved"},
            {"code": reasons.CAPACITY_COMPETITION},
        ]
        for reason in reason_dicts:
            text = describe_reason(reason)
            self.assertTrue(text)
            self.assertNotEqual(text, reason["code"])
