from datetime import date, time

from django.test import TestCase

from production.services.scheduler.pooling import (
    ResourcePool,
    pool_interchangeable_resources,
)
from production.services.scheduler.strategies.margin import MarginScheduler
from production.tests.factories import (
    machine_stage_resources,
    make_machine,
    make_priced_configuration,
    make_scheduling_world,
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

    def setUp(self):
        self.company, self.product, self.component = make_scheduling_world(
            stage=self.STAGE, slot_end=time(9, 0),
        )
        for i in (1, 2):
            make_machine(
                self.company, name=f"Sierra {i}", code=f"SIE-{i}",
                stages=[self.STAGE], daily_capacity_minutes=480, is_active=True,
            )

    def _create_configuration(self, name, price):
        return make_priced_configuration(
            product=self.product, component=self.component, name=name, price=price, cost=100,
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
