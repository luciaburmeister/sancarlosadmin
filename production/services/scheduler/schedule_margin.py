from configurator.models import Configuration
from production.services.scheduler.dict_var_creation import optimization_dict_creation
from production.services.scheduler.scheduler import ScheduleStrategy
from datetime import date
import pulp
from datetime import datetime, timedelta


class MarginScheduler(ScheduleStrategy):
    def __init__(self, optimization_params):
        self.ts = optimization_params["ts"]

    @staticmethod
    def _previous_slot_day(d, t, minutes=10):
        current_dt = datetime.combine(d, t)
        prev_dt = current_dt - timedelta(minutes=minutes)
        return prev_dt.date()

    @staticmethod
    def _previous_slot_time(d, t, minutes=10):
        current_dt = datetime.combine(d, t)
        prev_dt = current_dt - timedelta(minutes=minutes)
        return prev_dt.time()

    def _variable_definition(
        self,
        configurations,
        possible_days_weekday,
        configuration_company,
        company_times_by_weekday,
        configuration_items,
        item_stages,
        stage_resource_types,
        resource_type_active_resources,
    ) -> dict:

        x = {
            (c, i, s, d, t, r): pulp.LpVariable(
                f"x_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}", cat="Binary"
            )
            for c in configurations
            for i in configuration_items[c]
            for s in item_stages[i]
            for rt in stage_resource_types[(i, s)]
            for r in resource_type_active_resources[rt]
            for d, wd in possible_days_weekday.items()
            for t in company_times_by_weekday[configuration_company[c]][wd]
        }

        y = {c: pulp.LpVariable(f"y_{c.pk}", cat="Binary") for c in configurations}

        m = {
            (c, i, s, d, t, r): pulp.LpVariable(
                f"m_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}", cat="Binary"
            )
            for c in configurations
            for i in configuration_items[c]
            for s in item_stages[i]
            for rt in stage_resource_types[(i, s)]
            for r in resource_type_active_resources[rt]
            for d, wd in possible_days_weekday.items()
            for t in company_times_by_weekday[configuration_company[c]][wd]
        }

        variable_set = {"x": x, "y": y, "m": m}

        return variable_set

    def _objective_definition(self, variable_set, configuration_margin):
        objective = pulp.lpSum(
            variable_set["y"][c] * margin for c, margin in configuration_margin.ietms()
        )
        return objective

    def _constraint_definition(
        self,
        variable_set,
        configurations,
        possible_days_weekday,
        configuration_company,
        company_times_by_weekday,
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_duration,
        resource_type_active_resources,
        resource_parallel_capacity,
        resource_daily_capacity_minutes,
        ts,
    ):
        constraints = {}

        # If configuration assigned, then stages need to be assigned their duration
        constraints.update(
            {
                f"Duration_{c.pk}_{i.pk}_{s}_{rt}_lower_bound": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r)]
                        for d, wd in possible_days_weekday.items()
                        for t in company_times_by_weekday[configuration_company[c]][wd]
                        for r in resource_type_active_resources[rt]
                    )
                    * ts
                    >= variable_set["y"][c] * stage_duration[(i, s)],
                    f"Duration_{c.pk}_{i.pk}_{s}_lower_bound",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for rt in stage_resource_types[(i, s)]
            }
        )

        constraints.update(
            {
                f"Duration_{c.pk}_{i.pk}_{s}_{rt}_upper_bound": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r)]
                        for d, wd in possible_days_weekday.items()
                        for t in company_times_by_weekday[configuration_company[c]][wd]
                        for r in resource_type_active_resources[rt]
                    )
                    * ts
                    <= variable_set["y"][c] * stage_duration[(i, s)] + ts -1,
                    f"Duration_{c.pk}_{i.pk}_{s}_upper_bound",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for rt in stage_resource_types[(i, s)]
            }
        )

        # Stages assigned to up to one resource of each resource type per slot
        constraints.update(
            {
                f"Assignment_{c.pk}_{i.pk}_{s}_{d}_{t}_{rt}": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r)]
                        for r in resource_type_active_resources[rt]
                    )
                    <= variable_set["y"][c],
                    f"Assignment_{c.pk}_{i.pk}_{s}_{d}_{t}_{rt}",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for d, wd in possible_days_weekday.items()
                for t in company_times_by_weekday[configuration_company[c]][wd]
                for rt in stage_resource_types[(i, s)]
            }
        )

        # Stages can't change resource they use of each resource type
        constraints.update(
            {
                f"Movement_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_lower_bound": (
                    variable_set["m"][(c, i, s, d, t, r)]
                    >= variable_set["x"][(c, i, s, d, t, r)]
                    - variable_set["x"].get(
                        (
                            c,
                            i,
                            s,
                            self._previous_slot_day(d, t, 10),
                            self._previous_slot_time(d, t, 10),
                            r,
                        ),
                        0,
                    ),
                    f"Movement_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_lower_bound",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for d, wd in possible_days_weekday.items()
                for t in company_times_by_weekday[configuration_company[c]][wd]
                for rt in stage_resource_types[(i, s)]
                for r in resource_type_active_resources[rt]
            }
        )

        constraints.update(
            {
                f"Movement_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_upper_bound": (
                    variable_set["m"][(c, i, s, d, t, r)]
                    <= 2
                    - variable_set["x"][(c, i, s, d, t, r)]
                    - variable_set["x"].get(
                        (
                            c,
                            i,
                            s,
                            self._previous_slot_day(d, t, 10),
                            self._previous_slot_time(d, t, 10),
                            r,
                        ),
                        0,
                    ),
                    f"Movement_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_upper_bound",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for d, wd in possible_days_weekday.items()
                for t in company_times_by_weekday[configuration_company[c]][wd]
                for rt in stage_resource_types[(i, s)]
                for r in resource_type_active_resources[rt]
            }
        )

        constraints.update(
            {
                f"Movement_{c.pk}_{i.pk}_{s}_{r}_limit_to_one": (
                    pulp.lpSum(
                        variable_set["m"][(c, i, s, d, t, r)]
                        for d, wd in possible_days_weekday.items()
                        for t in company_times_by_weekday[configuration_company[c]][wd]
                    )
                    <= 1,
                    f"Movement_{c.pk}_{i.pk}_{s}_{r}_limit_to_one",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for rt in stage_resource_types[(i, s)]
                for r in resource_type_active_resources[rt]
            }
        )

        # Stages need to schedule all the resource types they need at the same time
        constraints.update(
            {
                f"Assign_all_resource_types_{c.pk}_{i.pk}_{s}_{d}_{t}_{rt}": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r)]
                        for r in resource_type_active_resources[stage_resource_types[(i, s)][0]]
                    )
                    == pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r)]
                        for r in resource_type_active_resources[rt]
                    ),
                    f"Assignment_{c.pk}_{i.pk}_{s}_{d}_{t}_{rt}",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for d, wd in possible_days_weekday.items()
                for t in company_times_by_weekday[configuration_company[c]][wd]
                for rt in stage_resource_types[(i, s)][1:]
            }
        )

        # Stages of an item need to be sequential (e.g. stage 2 can't start before 1 ends)
        company_ordered_slots = {
            company: [
                (d, t)
                for d, wd in sorted(possible_days_weekday.items())
                for t in company_times_by_weekday[company].get(wd, [])
            ]
            for company in {configuration_company[c] for c in configurations}
        }
        constraints.update(
            {
                f"Stage_sequence_{c.pk}_{i.pk}_{prev_s}_{s}_{d}_{t}": (
                    pulp.lpSum(
                        variable_set["x"].get((c, i, s, d, t, r), 0)
                        for r in resource_type_active_resources[stage_resource_types[(i, s)][0]]
                    ) * stage_duration[(i,prev_s)]
                    <=
                    pulp.lpSum(
                        variable_set["x"].get((c, i, prev_s, d_prev, t_prev, r_prev), 0)
                        for (d_prev, t_prev) in company_ordered_slots[configuration_company[c]][:pos]
                        for r_prev in resource_type_active_resources[stage_resource_types[(i, prev_s)][0]]
                    ) * ts,
                    f"Stage_sequence_{c.pk}_{i.pk}_{prev_s}_{s}_{d}_{t}",
                )
                for c in configurations
                for i in configuration_items[c]
                for stage_idx in range(1, len(item_stages[i]))
                for prev_s in [item_stages[i][stage_idx - 1]]
                for s in [item_stages[i][stage_idx]]
                for pos, (d, t) in enumerate(company_ordered_slots[configuration_company[c]])
            }
        )

        # Can't assign simulatenously to a resource more stages than its parallel_capacity
        constraints.update(
            {
                f"Parallel_capacity_{d}_{t}_{r.pk}": (
                    pulp.lpSum(
                        variable_set["x"].get((c, i, s, d, t, r), 0)
                        for c in configurations
                        for i in configuration_items[c]
                        for s in item_stages[i]
                    )
                    <= resource_parallel_capacity[r],
                    f"Parallel_capacity_{d}_{t}_{r.pk}",
                )
                for d, wd in possible_days_weekday.items()
                for rt in resource_type_active_resources.keys()
                for r in resource_type_active_resources[rt]
                for t in company_times_by_weekday[r.company].get(wd, [])
            }
        )

        # The sum of stages assigned to a resource per day need to be less than its daily_capacity_minutes
        # TODO: right now stages running in parallel each count, could make it so that they count as 1? (would need to add another variable -> more complexity & time to run)
        constraints.update(
            {
                f"Daily_capacity_minutes_{d}_{r.pk}": (
                    pulp.lpSum(
                        variable_set["x"].get((c, i, s, d, t, r), 0)
                        for c in configurations
                        for i in configuration_items[c]
                        for s in item_stages[i]
                        for t in company_times_by_weekday[configuration_company[c]][wd]
                    ) * ts
                    <= resource_daily_capacity_minutes[r],
                    f"Daily_capacity_minutes_{d}_{r.pk}",
                )
                for rt in resource_type_active_resources.keys()
                for r in resource_type_active_resources[rt]
                for d, wd in possible_days_weekday.items()
            }
        )

        return constraints
    
    def _solve_problem(self, objective: pulp.LpAffineExpression, constraints: dict) -> pulp.LpProblem:
        prob = pulp.LpProblem("Schedule configuration stages to resources", pulp.LpMaximize)
        prob += objective

        for c in constraints.values():
            prob += c

        prob.solve()
        return prob
    
    def schedule(
        self,
        start_date: date,
        end_date: date,
        configurations: list[Configuration],
    ) -> None:
        """Optimizes total margin gained in given time period.

        Parameters
        ----------
        start_date : date
            Date from which the optimizer can assign slots
        end_date : date
            Date to which the optimizer can assign slots
        configurations : list[Configuration]
            List of configurations for which to assign production slots
        """

        (
            possible_days_weekday,
            configuration_company,
            company_times_by_weekday,
            configuration_margin,
            configuration_items,
            item_stages,
            stage_resource_types,
            stage_duration,
            resource_type_active_resources,
            resource_parallel_capacity,
            resource_daily_capacity_minutes,
        ) = optimization_dict_creation(start_date, end_date, configurations, self.ts)

        variable_set = self._variable_definition(
            configurations,
            possible_days_weekday,
            configuration_company,
            company_times_by_weekday,
            configuration_items,
            item_stages,
            stage_resource_types,
            resource_type_active_resources
        )
        objective = self._objective_definition(variable_set, configuration_margin)
        constraints = self._constraint_definition(
            variable_set,
            configurations,
            possible_days_weekday,
            configuration_company,
            company_times_by_weekday,
            configuration_items,
            item_stages,
            stage_resource_types,
            stage_duration,
            resource_type_active_resources,
            resource_parallel_capacity,
            resource_daily_capacity_minutes,
            self.ts,
        )

        prob = self._solve_problem(objective, constraints)
