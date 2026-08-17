"""Temporary benchmark: where does MarginScheduler.schedule() spend time?

Run with: poetry run python manage.py bench? -> no, run:
  poetry run python manage.py test production.tests.bench_scheduler -v 2
"""
import time as time_mod
from datetime import date, time

import pulp
from django.test import TestCase
from djmoney.money import Money

from company.models import Company, CompanyScheduleSlot, Employee, Machine
from configurator.models import Configuration, ConfigurationItem, ProductTemplate
from materials.models import Component
from production.services.scheduler.data import optimization_dict_creation
from production.services.scheduler.strategies.margin import MarginScheduler

MONDAY = date(2026, 8, 10)
END = date(2026, 8, 21)  # 10 weekdays


class SchedulerBenchmark(TestCase):
    def _build_world(self, *, n_configs, n_stages, n_machines, n_employees):
        stages = [f"stage{i}" for i in range(n_stages)]
        company = Company.objects.create(name="Bench Co", production_stages=stages)
        for wd in range(5):
            CompanyScheduleSlot.objects.create(
                company=company, weekday=wd,
                start_time=time(8, 0), end_time=time(18, 0),
            )
        product = ProductTemplate.objects.create(
            name="Bench product", company=company, production_stages=stages,
        )
        component = Component.objects.create(
            company=company, name="C", unit_price_expression="0",
        )
        for m in range(n_machines):
            Machine.objects.create(
                company=company, name=f"M{m}", code=f"M{m}",
                production_stages=stages, daily_capacity_minutes=600,
            )
        for e in range(n_employees):
            Employee.objects.create(
                company=company, first_name=f"E{e}", last_name="Bench",
                hired_at=date(2020, 1, 1), can_work_on_production=True,
                production_stages=stages,
            )
        stage_resources = {
            s: {
                "machine_types": [{"quantity": 1}],
                "employee_types": [{"quantity": 1}],
                "prep_time_min": 0, "work_time_min": 60, "post_time_min": 0,
            }
            for s in stages
        }
        configs = []
        for i in range(n_configs):
            c = Configuration.objects.create(
                company=company, product=product, name=f"cfg{i}",
                total_price=Money(1000 + i, "EUR"), total_cost=Money(400, "EUR"),
            )
            ConfigurationItem.objects.create(
                configuration=c, component=component, unit_price=Money(0, "EUR"),
                is_template_item=True, production_stages=stages,
                stage_resources=stage_resources,
            )
            configs.append(c)
        return configs

    def _bench(self, label, *, n_configs, n_stages, n_machines, n_employees, ts):
        configs = self._build_world(
            n_configs=n_configs, n_stages=n_stages,
            n_machines=n_machines, n_employees=n_employees,
        )
        sch = MarginScheduler(optimization_params={"ts": ts})
        t = {}

        t0 = time_mod.perf_counter()
        (company_ordered_slots, configuration_company, configuration_margin,
         configuration_stages, stage_resource_types, stage_resource_type_quantity,
         stage_duration, resource_type_active_resources, resource_parallel_capacity,
         resource_daily_capacity_minutes, resource_slot_load, resource_day_load,
         ) = optimization_dict_creation(MONDAY, END, configs, ts)
        t["data"] = time_mod.perf_counter() - t0

        stage_required_slots = sch._stage_required_slots(stage_duration, ts)
        stage_suffix_slots = sch._config_stage_suffix_slots(configuration_stages, stage_required_slots)
        stage_prefix_slots = sch._config_stage_prefix_slots(configuration_stages, stage_required_slots)
        valid_start_positions = sch._valid_stage_start_positions(
            company_ordered_slots, configuration_company, configs,
            configuration_stages, stage_suffix_slots, stage_prefix_slots)
        stage_position_windows = sch._stage_position_windows(
            company_ordered_slots, configuration_company, configs,
            configuration_stages, stage_required_slots,
            stage_prefix_slots, stage_suffix_slots)
        covering_start_positions = sch._covering_start_positions(
            valid_start_positions, stage_required_slots)

        from production.services.scheduler.pooling import pool_interchangeable_resources
        (resource_type_active_resources, resource_parallel_capacity,
         resource_daily_capacity_minutes, resource_slot_load, resource_day_load,
         ) = pool_interchangeable_resources(
            resource_type_active_resources, resource_parallel_capacity,
            resource_daily_capacity_minutes, resource_slot_load, resource_day_load)

        t0 = time_mod.perf_counter()
        variable_set = sch._variable_definition(
            configs, configuration_company, company_ordered_slots,
            configuration_stages, stage_resource_types,
            stage_resource_type_quantity, resource_type_active_resources,
            valid_start_positions, stage_position_windows)
        t["variables"] = time_mod.perf_counter() - t0

        t0 = time_mod.perf_counter()
        use_warm = sch._should_use_warm_start(
            configs, configuration_company, company_ordered_slots,
            configuration_stages, stage_resource_types,
            stage_resource_type_quantity, resource_type_active_resources)
        if use_warm:
            sch._apply_greedy_warm_start(
                variable_set, configs, configuration_margin, configuration_company,
                company_ordered_slots, configuration_stages, stage_resource_types,
                stage_resource_type_quantity, stage_required_slots,
                valid_start_positions, resource_type_active_resources)
        t["warm_start"] = time_mod.perf_counter() - t0

        objective = sch._objective_definition(
            variable_set, configuration_margin, configs,
            configuration_stages, valid_start_positions)

        t0 = time_mod.perf_counter()
        constraints = sch._constraint_definition(
            variable_set, configs, configuration_company, company_ordered_slots,
            configuration_stages, stage_resource_types, stage_resource_type_quantity,
            stage_required_slots, valid_start_positions, covering_start_positions,
            resource_type_active_resources, resource_parallel_capacity,
            resource_daily_capacity_minutes, ts, resource_slot_load, resource_day_load,
            stage_position_windows)
        t["constraints"] = time_mod.perf_counter() - t0

        t0 = time_mod.perf_counter()
        prob = pulp.LpProblem("bench", pulp.LpMaximize)
        prob += objective
        for con in constraints.values():
            prob += con
        t["add_to_problem"] = time_mod.perf_counter() - t0

        t0 = time_mod.perf_counter()
        solver = sch._make_solver(use_warm, 60, 0.05, None, None)
        prob.solve(solver)
        t["solve_total"] = time_mod.perf_counter() - t0
        cbc_time = prob.solutionTime

        n_slots = len(next(iter(company_ordered_slots.values())))
        print(f"\n=== {label}: {n_configs} configs x {n_stages} stages, "
              f"{n_machines}+{n_employees} resources, ts={ts}, slots={n_slots}")
        print(f"  x vars: {len(variable_set['x'])}, constraints: {len(constraints)}, "
              f"status: {pulp.LpStatus[prob.status]}")
        for k, v in t.items():
            print(f"  {k:>14}: {v:8.2f}s")
        print(f"  (cbc reported solve: {cbc_time:.2f}s)")

    def test_bench_small(self):
        self._bench("small", n_configs=5, n_stages=3, n_machines=2,
                    n_employees=2, ts=30)

    def test_bench_medium(self):
        self._bench("medium", n_configs=10, n_stages=6, n_machines=3,
                    n_employees=3, ts=30)
