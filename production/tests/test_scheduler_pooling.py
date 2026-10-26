from datetime import date, datetime, time

from django.test import TestCase
from django.utils import timezone

from company.models import Machine
from materials.models import Component
from production.services.scheduler.pooling import (
    ResourcePool,
    pool_interchangeable_resources,
)
from production.services.scheduler.strategies.margin import MarginScheduler
from production.tests.factories import (
    add_schedule,
    machine_stage_resources,
    make_company,
    make_machine,
    make_priced_configuration,
    make_product,
    make_scheduling_world,
    make_session,
    make_ticket,
    resource_for,
)

# 2026-08-10 is a Monday
MONDAY = date(2026, 8, 10)


class FakeResource:
    def __init__(self, pk, company_id=1):
        self.pk = pk
        self.company_id = company_id

    def __repr__(self):
        return f"R{self.pk}"

    def __str__(self):
        return f"R{self.pk}"

    def __hash__(self):
        return hash(self.pk)

    def __eq__(self, other):
        return isinstance(other, FakeResource) and other.pk == self.pk


class PoolInterchangeableResourcesTest(TestCase):
    def test_identical_resources_are_pooled(self):
        rt = ("machine_ct", "corte")
        r1, r2, r3 = FakeResource(1), FakeResource(2), FakeResource(3)
        different_cap = FakeResource(4)

        (pooled_rt, parallel, daily, slot_load, day_load) = pool_interchangeable_resources(
            resource_type_active_resources={rt: [r1, r2, r3, different_cap]},
            resource_parallel_capacity={r1: 1, r2: 1, r3: 1, different_cap: 2},
            resource_daily_capacity_minutes={r1: 480, r2: 480, r3: 480, different_cap: 480},
            resource_slot_load={(r1, 5): 1, (r2, 5): 1},
            resource_day_load={(r1, MONDAY): 60},
        )

        pools = pooled_rt[rt]
        self.assertEqual(len(pools), 2)
        big, single = pools
        self.assertEqual([m.pk for m in big.members], [1, 2, 3])
        self.assertEqual(parallel[big], 3)
        self.assertEqual(daily[big], 3 * 480)
        self.assertEqual(single.members, [different_cap])
        self.assertEqual(parallel[single], 2)
        # Existing loads aggregate onto the pool
        self.assertEqual(slot_load[(big, 5)], 2)
        self.assertEqual(day_load[(big, MONDAY)], 60)

    def test_resources_with_different_stage_sets_are_not_pooled(self):
        rt_corte = ("machine_ct", "corte")
        rt_mec = ("machine_ct", "mecanizado")
        both_stages = FakeResource(1)
        corte_only = FakeResource(2)

        (pooled_rt, *_rest) = pool_interchangeable_resources(
            resource_type_active_resources={
                rt_corte: [both_stages, corte_only],
                rt_mec: [both_stages],
            },
            resource_parallel_capacity={both_stages: 1, corte_only: 1},
            resource_daily_capacity_minutes={both_stages: 480, corte_only: 480},
            resource_slot_load={},
            resource_day_load={},
        )

        # Pooling them would let the shared machine be double-booked across
        # stages, so they must stay separate.
        self.assertEqual(len(pooled_rt[rt_corte]), 2)
        self.assertEqual(len(pooled_rt[rt_mec]), 1)


class AssignPoolMembersTest(TestCase):
    def setUp(self):
        self.scheduler = MarginScheduler(optimization_params={"ts": 60})
        self.company = object()
        self.slots = [(MONDAY, time(8 + i, 0)) for i in range(4)]

    def _usage(self, pool, positions):
        return {
            "configuration": type("C", (), {"pk": 1, "__str__": lambda s: "cfg"})(),
            "company": self.company,
            "stage": "corte",
            "pool": pool,
            "positions": positions,
        }

    def test_prefers_a_single_member_for_the_whole_interval(self):
        m1, m2 = FakeResource(1), FakeResource(2)
        pool = ResourcePool(pk="pool0", members=[m1, m2], parallel_capacity=2)

        assignments, warnings = self.scheduler._assign_pool_members(
            [self._usage(pool, [0, 1, 2])],
            {self.company: self.slots},
            member_parallel_capacity={m1: 1, m2: 1},
            member_slot_load={(m1, 1): 1},  # m1 busy in the middle
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].resource_id, 2)
        self.assertEqual(assignments[0].slot_count, 3)

    def test_parallel_usages_get_distinct_members(self):
        m1, m2 = FakeResource(1), FakeResource(2)
        pool = ResourcePool(pk="pool0", members=[m1, m2], parallel_capacity=2)

        assignments, warnings = self.scheduler._assign_pool_members(
            [self._usage(pool, [0, 1]), self._usage(pool, [0, 1])],
            {self.company: self.slots},
            member_parallel_capacity={m1: 1, m2: 1},
            member_slot_load={},
        )

        self.assertEqual(warnings, [])
        self.assertEqual({a.resource_id for a in assignments}, {1, 2})

    def test_splits_across_members_when_no_single_member_fits(self):
        m1, m2 = FakeResource(1), FakeResource(2)
        pool = ResourcePool(pk="pool0", members=[m1, m2], parallel_capacity=2)

        assignments, warnings = self.scheduler._assign_pool_members(
            [self._usage(pool, [0, 1])],
            {self.company: self.slots},
            member_parallel_capacity={m1: 1, m2: 1},
            member_slot_load={(m1, 1): 1, (m2, 0): 1},
        )

        self.assertEqual(len(warnings), 1)
        self.assertEqual(len(assignments), 2)
        self.assertEqual(
            [(a.resource_id, a.slot_count) for a in assignments], [(1, 1), (2, 1)]
        )


class PooledSchedulingEndToEndTest(TestCase):
    """Two identical machines, two configurations competing for one slot."""

    STAGE = "corte"

    @classmethod
    def setUpTestData(cls):
        cls.company, cls.product, cls.component = make_scheduling_world(
            stage=cls.STAGE, slot_end=time(9, 0),
        )
        for i in (1, 2):
            make_machine(
                company=cls.company, name=f"Sierra {i}", code=f"SIE-{i}",
                stages=[cls.STAGE], daily_capacity_minutes=480,
                is_active=True,
            )

    def _create_configuration(self, name, price, *, company=None):
        company = company or self.company
        product = self.product
        component = self.component
        if company != self.company:
            product = make_product(
                company, name="Window", stages=[self.STAGE],
            )
            component = Component.objects.create(
                company=company, name="Profile", unit_price_expression="0",
            )
        return make_priced_configuration(
            product=product, component=component, name=name, price=price, cost=100,
            stages=[self.STAGE], stage_resources=machine_stage_resources([self.STAGE]),
        )

    def test_both_configurations_share_the_slot_on_distinct_machines(self):
        c1 = self._create_configuration("A", 1000)
        c2 = self._create_configuration("B", 900)

        scheduler = MarginScheduler(optimization_params={"ts": 60})
        proposal = scheduler.schedule(
            start_date=MONDAY, end_date=MONDAY, configurations=[c1, c2],
        )

        self.assertEqual(
            set(proposal.selected_configuration_ids), {c1.pk, c2.pk}
        )
        self.assertEqual(len(proposal.assignments), 2)
        # Concrete, distinct machines — not the pool
        resource_ids = {a.resource_id for a in proposal.assignments}
        self.assertEqual(len(resource_ids), 2)
        for a in proposal.assignments:
            self.assertIsInstance(a.resource_id, int)

    def test_other_company_machine_is_not_available_to_this_company(self):
        other = make_company(name="Other company", stages=[self.STAGE])
        Machine.objects.filter(company=self.company).update(is_active=False)
        make_machine(
            company=other, name="Other oven", code="OTHER-OVEN", needs_human=False,
            stages=[self.STAGE], daily_capacity_minutes=60, parallel_capacity=2,
        )
        configuration = self._create_configuration("A", 1000)

        proposal = MarginScheduler(optimization_params={"ts": 60}).schedule(
            MONDAY, MONDAY, [configuration],
        )

        self.assertEqual(proposal.selected_configuration_ids, [])
        self.assertEqual(proposal.assignments, [])

    def test_multi_company_run_keeps_resource_pools_separate(self):
        other = make_company(name="Other company", stages=[self.STAGE])
        add_schedule(other, weekdays=[0], ranges=((time(8), time(9)),))
        Machine.objects.filter(company=self.company).update(is_active=False)
        oven = make_machine(
            company=other, name="Other oven", code="OTHER-OVEN", needs_human=False,
            stages=[self.STAGE], daily_capacity_minutes=60, parallel_capacity=2,
        )
        own_configuration = self._create_configuration("A", 1000)
        other_configuration = self._create_configuration("B", 900, company=other)

        proposal = MarginScheduler(optimization_params={"ts": 60}).schedule(
            MONDAY, MONDAY, [own_configuration, other_configuration],
        )

        self.assertEqual(proposal.selected_configuration_ids, [other_configuration.pk])
        self.assertEqual(proposal.assignments[0].resource_id, resource_for(oven).pk)

    def test_unattended_machine_shares_running_time_across_parallel_jobs(self):
        machine, unused_machine = Machine.objects.filter(company=self.company).order_by("pk")
        machine.needs_human = False
        machine.parallel_capacity = 2
        machine.daily_capacity_minutes = 60
        machine.save(update_fields=["needs_human", "parallel_capacity", "daily_capacity_minutes"])
        unused_machine.is_active = False
        unused_machine.save(update_fields=["is_active"])
        first = self._create_configuration("A", 1000)
        second = self._create_configuration("B", 900)
        scheduler = MarginScheduler(optimization_params={"ts": 60})

        proposal = scheduler.schedule(MONDAY, MONDAY, [first, second])

        self.assertEqual(set(proposal.selected_configuration_ids), {first.pk, second.pk})
        self.assertEqual(len({assignment.resource_id for assignment in proposal.assignments}), 1)

        machine.needs_human = True
        machine.save(update_fields=["needs_human"])
        proposal = scheduler.schedule(MONDAY, MONDAY, [first, second])
        self.assertEqual(len(proposal.selected_configuration_ids), 1)

    def test_existing_unattended_job_uses_one_parallel_place_and_one_hour(self):
        machine, unused_machine = Machine.objects.filter(company=self.company).order_by("pk")
        machine.needs_human = False
        machine.parallel_capacity = 2
        machine.daily_capacity_minutes = 60
        machine.save(update_fields=["needs_human", "parallel_capacity", "daily_capacity_minutes"])
        unused_machine.is_active = False
        unused_machine.save(update_fields=["is_active"])
        ticket = make_ticket(self.company, stages=[self.STAGE], machine_qty=1, employee_qty=0)
        make_session(
            ticket, stage=self.STAGE,
            start=timezone.make_aware(datetime.combine(MONDAY, time(8, 0))),
            end=timezone.make_aware(datetime.combine(MONDAY, time(9, 0))),
            resources=[resource_for(machine)],
        )
        first = self._create_configuration("A", 1000)
        second = self._create_configuration("B", 900)

        proposal = MarginScheduler(optimization_params={"ts": 60}).schedule(
            MONDAY, MONDAY, [first, second],
        )

        self.assertEqual(len(proposal.selected_configuration_ids), 1)
        self.assertEqual(proposal.assignments[0].resource_id, resource_for(machine).pk)

    def test_partial_existing_job_adds_only_uncovered_running_minutes(self):
        machine, unused_machine = Machine.objects.filter(company=self.company).order_by("pk")
        machine.needs_human = False
        machine.parallel_capacity = 2
        machine.daily_capacity_minutes = 45
        machine.save(update_fields=["needs_human", "parallel_capacity", "daily_capacity_minutes"])
        unused_machine.is_active = False
        unused_machine.save(update_fields=["is_active"])
        ticket = make_ticket(self.company, stages=[self.STAGE], machine_qty=1, employee_qty=0)
        make_session(
            ticket, stage=self.STAGE,
            start=timezone.make_aware(datetime.combine(MONDAY, time(8, 0))),
            end=timezone.make_aware(datetime.combine(MONDAY, time(8, 30))),
            resources=[resource_for(machine)],
        )
        configuration = self._create_configuration("A", 1000)
        scheduler = MarginScheduler(optimization_params={"ts": 60})

        self.assertEqual(
            scheduler.schedule(MONDAY, MONDAY, [configuration]).selected_configuration_ids,
            [],
        )
        machine.daily_capacity_minutes = 60
        machine.save(update_fields=["daily_capacity_minutes"])
        self.assertEqual(
            scheduler.schedule(MONDAY, MONDAY, [configuration]).selected_configuration_ids,
            [configuration.pk],
        )
