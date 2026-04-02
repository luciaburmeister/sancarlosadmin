from configurator.models import Configuration
from production.services.scheduler.dict_var_creation import optimization_dict_creation
from production.services.scheduler.schedule_proposal import ScheduleProposal, ScheduledAssignment
from production.services.scheduler.scheduler import ScheduleStrategy
from datetime import date
import pulp
from datetime import datetime, timedelta
from typing import Any

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
    
    @staticmethod
    def _var_value_is_one(var: pulp.LpVariable, tol: float = 1e-5) -> bool:
        value = pulp.value(var)
        return value is not None and value >= 1 - tol

    def _variable_definition(
        self,
        configurations,
        possible_days_weekday,
        configuration_company,
        company_times_by_weekday,
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        resource_type_active_resources,
    ) -> dict:

        x = {
            (c, i, s, d, t, r, q): pulp.LpVariable(
                f"x_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_{q}", cat="Binary"
            )
            for c in configurations
            for i in configuration_items[c]
            for s in item_stages[i]
            for rt in stage_resource_types[(i, s)]
            for q in range(stage_resource_type_quantity[(i, s, rt)])
            for r in resource_type_active_resources[rt]
            for d, wd in possible_days_weekday.items()
            for t in company_times_by_weekday[configuration_company[c]][wd]
        }

        y = {c: pulp.LpVariable(f"y_{c.pk}", cat="Binary") for c in configurations}

        m = {
            (c, i, s, d, t, r, q): pulp.LpVariable(
                f"m_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_{q}", cat="Binary"
            )
            for c in configurations
            for i in configuration_items[c]
            for s in item_stages[i]
            for rt in stage_resource_types[(i, s)]
            for q in range(stage_resource_type_quantity[(i, s, rt)])
            for r in resource_type_active_resources[rt]
            for d, wd in possible_days_weekday.items()
            for t in company_times_by_weekday[configuration_company[c]][wd]
        }

        variable_set = {"x": x, "y": y, "m": m}

        return variable_set

    def _objective_definition(self, variable_set, configuration_margin):
        objective = pulp.lpSum(
            variable_set["y"][c] * margin for c, margin in configuration_margin.items()
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
        stage_resource_type_quantity,
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
                f"Duration_{c.pk}_{i.pk}_{s}_lower_bound": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r, 0)]
                        for d, wd in possible_days_weekday.items()
                        for t in company_times_by_weekday[configuration_company[c]][wd]
                        for r in resource_type_active_resources[stage_resource_types[(i, s)][0]]
                    ) * ts
                    >= variable_set["y"][c] * stage_duration[(i, s)],
                    f"Duration_{c.pk}_{i.pk}_{s}_lower_bound",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                if stage_resource_types[(i, s)]
            }
        )

        constraints.update(
            {
                f"Duration_{c.pk}_{i.pk}_{s}_upper_bound": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r, 0)]
                        for d, wd in possible_days_weekday.items()
                        for t in company_times_by_weekday[configuration_company[c]][wd]
                        for r in resource_type_active_resources[stage_resource_types[(i, s)][0]]
                    ) * ts
                    <= variable_set["y"][c] * stage_duration[(i, s)] + ts -1,
                    f"Duration_{c.pk}_{i.pk}_{s}_upper_bound",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
            }
        )

        # Stages assigned to up to one resource of each (resource type, q) per slot
        constraints.update(
            {
                f"Assignment_{c.pk}_{i.pk}_{s}_{d}_{t}_{rt[0].pk}_{rt[1]}_{q}": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r, q)]
                        for r in resource_type_active_resources[rt]
                    )
                    <= variable_set["y"][c],
                    f"Assignment_{c.pk}_{i.pk}_{s}_{d}_{t}_{rt[0].pk}_{rt[1]}_{q}",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for d, wd in possible_days_weekday.items()
                for t in company_times_by_weekday[configuration_company[c]][wd]
                for rt in stage_resource_types[(i, s)]
                for q in range(stage_resource_type_quantity[(i, s, rt)])
            }
        )

        # Stages can't change resource they use of each resource type
        constraints.update(
            {
                f"Movement_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_{q}_lower_bound": (
                    variable_set["m"][(c, i, s, d, t, r, q)]
                    >= variable_set["x"][(c, i, s, d, t, r, q)]
                    - variable_set["x"].get(
                        (
                            c,
                            i,
                            s,
                            self._previous_slot_day(d, t, self.ts),
                            self._previous_slot_time(d, t, self.ts),
                            r,
                            q,
                        ),
                        0,
                    ),
                    f"Movement_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_{q}_lower_bound",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for d, wd in possible_days_weekday.items()
                for t in company_times_by_weekday[configuration_company[c]][wd]
                for rt in stage_resource_types[(i, s)]
                for r in resource_type_active_resources[rt]
                for q in range(stage_resource_type_quantity[(i, s, rt)])
            }
        )

        constraints.update(
            {
                f"Movement_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_{q}_upper_bound": (
                    variable_set["m"][(c, i, s, d, t, r, q)]
                    <= 2
                    - variable_set["x"][(c, i, s, d, t, r, q)]
                    - variable_set["x"].get(
                        (
                            c,
                            i,
                            s,
                            self._previous_slot_day(d, t, self.ts),
                            self._previous_slot_time(d, t, self.ts),
                            r,
                            q,
                        ),
                        0,
                    ),
                    f"Movement_{c.pk}_{i.pk}_{s}_{d}_{t}_{r.pk}_{q}_upper_bound",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for d, wd in possible_days_weekday.items()
                for t in company_times_by_weekday[configuration_company[c]][wd]
                for rt in stage_resource_types[(i, s)]
                for r in resource_type_active_resources[rt]
                for q in range(stage_resource_type_quantity[(i, s, rt)])
            }
        )

        constraints.update(
            {
                f"Movement_{c.pk}_{i.pk}_{s}_{rt[0].pk}_{rt[1]}_{q}_limit_to_one": (
                    pulp.lpSum(
                        variable_set["m"][(c, i, s, d, t, r, q)]
                        for d, wd in possible_days_weekday.items()
                        for t in company_times_by_weekday[configuration_company[c]][wd]
                        for r in resource_type_active_resources[rt]
                    )
                    <= 1,
                    f"Movement_{c.pk}_{i.pk}_{s}_{rt[0].pk}_{rt[1]}_{q}_limit_to_one",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for rt in stage_resource_types[(i, s)]
                for q in range(stage_resource_type_quantity[(i, s, rt)])
            }
        )

        # Stages need to schedule all the resource types they need at the same time
        constraints.update(
            {
                f"Assign_all_resource_types_{c.pk}_{i.pk}_{s}_{d}_{t}_{rt[0].pk}_{rt[1]}_{q}": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r, 0)]
                        for r in resource_type_active_resources[stage_resource_types[(i, s)][0]]
                    )
                    == pulp.lpSum(
                        variable_set["x"][(c, i, s, d, t, r, q)]
                        for r in resource_type_active_resources[rt]
                    ),
                    f"Assign_all_resource_types_{c.pk}_{i.pk}_{s}_{d}_{t}_{rt[0].pk}_{rt[1]}_{q}",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for d, wd in possible_days_weekday.items()
                for t in company_times_by_weekday[configuration_company[c]][wd]
                for rt in stage_resource_types[(i, s)]
                for q in range(stage_resource_type_quantity[(i, s, rt)])
                if stage_resource_types[(i, s)]
                if not (rt == stage_resource_types[(i, s)][0] and q == 0)
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
                        variable_set["x"].get((c, i, s, d, t, r, 0), 0)
                        for r in resource_type_active_resources[stage_resource_types[(i, s)][0]]
                    ) * stage_duration[(i,prev_s)]
                    <=
                    pulp.lpSum(
                        variable_set["x"].get((c, i, prev_s, d_prev, t_prev, r_prev, 0), 0)
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

        # Can't assign simultaneously to a resource more stages than its parallel_capacity
        constraints.update(
            {
                f"Parallel_capacity_{d}_{t}_{r.pk}": (
                    pulp.lpSum(
                        variable_set["x"].get((c, i, s, d, t, r, q), 0)
                        for c in configurations
                        for i in configuration_items[c]
                        for s in item_stages[i]
                        for rt in stage_resource_types[(i, s)]
                        for q in range(stage_resource_type_quantity[(i, s, rt)])
                    )
                    <= parallel_capacity,
                    f"Parallel_capacity_{d}_{t}_{r.pk}",
                )
                for d, wd in possible_days_weekday.items()
                for r, parallel_capacity in resource_parallel_capacity.items()
                for t in company_times_by_weekday[r.company].get(wd, [])
            }
        )

        # The sum of stages assigned to a resource per day need to be less than its daily_capacity_minutes
        # TODO: right now stages running in parallel each count, could make it so that they count as 1? (would need to add another variable -> more complexity & time to run)
        constraints.update(
            {
                f"Daily_capacity_minutes_{d}_{r.pk}": (
                    pulp.lpSum(
                        variable_set["x"].get((c, i, s, d, t, r, q), 0)
                        for c in configurations
                        for i in configuration_items[c]
                        for s in item_stages[i]
                        for rt in stage_resource_types[(i, s)]
                        for q in range(stage_resource_type_quantity[(i, s, rt)])
                        for t in company_times_by_weekday[configuration_company[c]][wd]
                    ) * ts
                    <= daily_capacity_minutes,
                    f"Daily_capacity_minutes_{d}_{r.pk}",
                )
                for r, daily_capacity_minutes in resource_daily_capacity_minutes.items()
                for d, wd in possible_days_weekday.items()
            }
        )

        return constraints
    
    def _merge_consecutive_slots(
        self,
        slots: list[tuple[date, Any]],
    ) -> list[tuple[datetime, datetime, int]]:
        """
        Merge consecutive (date, time) slots into continuous intervals.

        Returns
        -------
        list[tuple[datetime, datetime, int]]
            Each tuple is (start_datetime, end_datetime, slot_count)
        """
        if not slots:
            return []

        ordered = sorted(slots, key=lambda x: (x[0], x[1]))
        intervals = []

        current_start = datetime.combine(ordered[0][0], ordered[0][1])
        current_end = current_start + timedelta(minutes=self.ts)
        slot_count = 1

        for d, t in ordered[1:]:
            dt = datetime.combine(d, t)
            expected_next = current_end

            if dt == expected_next:
                current_end = dt + timedelta(minutes=self.ts)
                slot_count += 1
            else:
                intervals.append((current_start, current_end, slot_count))
                current_start = dt
                current_end = dt + timedelta(minutes=self.ts)
                slot_count = 1

        intervals.append((current_start, current_end, slot_count))
        return intervals

    def _extract_solution(
        self,
        prob: pulp.LpProblem,
        variable_set: dict,
        configurations,
        configuration_margin,
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        resource_type_active_resources,
    ) -> dict:
        """
        Extract selected configurations and concrete stage-resource intervals
        from the solved PuLP model.
        """
        solver_status = pulp.LpStatus.get(prob.status, str(prob.status))
        is_optimal = solver_status == "Optimal"
        objective_value = pulp.value(prob.objective)

        selected_configurations = []
        unselected_configurations = []
        assignments = []
        warnings = []

        if solver_status not in {"Optimal", "Not Solved", "Undefined", "Infeasible", "Unbounded", "Integer Feasible"}:
            warnings.append(f"Unexpected solver status: {solver_status}")

        if solver_status == "Infeasible":
            warnings.append("The optimization problem is infeasible.")
        elif solver_status == "Unbounded":
            warnings.append("The optimization problem is unbounded.")
        elif solver_status == "Not Solved":
            warnings.append("The solver did not finish with a solved status.")
        elif solver_status == "Undefined":
            warnings.append("The solver returned an undefined status.")
        elif solver_status == "Integer Feasible":
            warnings.append("A feasible integer solution was found, but optimality was not proven.")

        # Read selected / unselected configurations
        for c in configurations:
            if self._var_value_is_one(variable_set["y"][c]):
                selected_configurations.append(c)
            else:
                unselected_configurations.append(c)

        # For every selected configuration, collect active slots by (item, stage, resource, q)
        for c in selected_configurations:
            for i in configuration_items[c]:
                for s in item_stages[i]:
                    if not stage_resource_types[(i, s)]:
                        continue

                    # Collect all relevant (resource, q) combinations for this stage
                    seen_resource_q_pairs = set()
                    for rt in stage_resource_types[(i, s)]:
                        for q in range(stage_resource_type_quantity[(i, s, rt)]):
                            for r in resource_type_active_resources[rt]:
                                key = (r, q)
                                if key in seen_resource_q_pairs:
                                    continue
                                seen_resource_q_pairs.add(key)

                                active_slots = []
                                for (c2, i2, s2, d, t, r2, q2), var in variable_set["x"].items():
                                    if c2 == c and i2 == i and s2 == s and r2 == r and q2 == q:
                                        if self._var_value_is_one(var):
                                            active_slots.append((d, t))

                                if not active_slots:
                                    continue

                                merged_intervals = self._merge_consecutive_slots(active_slots)

                                for start_dt, end_dt, slot_count in merged_intervals:
                                    assignments.append(
                                        ScheduledAssignment(
                                            configuration_id=c.pk,
                                            configuration_name=str(c),
                                            configuration_item_id=i.pk,
                                            item_name=str(i),
                                            stage_name=s,
                                            resource_id=r.pk,
                                            resource_name=str(r),
                                            copy_index=q,
                                            start=start_dt,
                                            end=end_dt,
                                            slot_count=slot_count,
                                            minutes=slot_count * self.ts,
                                        )
                                    )

        # Sort assignments for nicer downstream display
        assignments.sort(
            key=lambda a: (
                a.start,
                a.configuration_id,
                a.configuration_item_id,
                a.stage_name,
                a.resource_id,
                a.copy_index,
            )
        )

        total_margin = sum(float(configuration_margin[c]) for c in selected_configurations)

        return {
            "solver_status": solver_status,
            "is_optimal": is_optimal,
            "objective_value": float(objective_value) if objective_value is not None else None,
            "total_margin": float(total_margin),
            "selected_configurations": selected_configurations,
            "unselected_configurations": unselected_configurations,
            "assignments": assignments,
            "warnings": warnings,
        }

    def _build_proposal(
        self,
        start_date: date,
        end_date: date,
        configuration_margin,
        extracted_solution: dict,
    ) -> ScheduleProposal:
        selected_configurations = extracted_solution["selected_configurations"]
        unselected_configurations = extracted_solution["unselected_configurations"]

        return ScheduleProposal(
            start_date=start_date,
            end_date=end_date,
            slot_minutes=self.ts,
            solver_status=extracted_solution["solver_status"],
            is_optimal=extracted_solution["is_optimal"],
            objective_value=extracted_solution["objective_value"],
            total_margin=extracted_solution["total_margin"],
            selected_configuration_ids=[c.pk for c in selected_configurations],
            unselected_configuration_ids=[c.pk for c in unselected_configurations],
            configuration_margins={
                c.pk: float(configuration_margin[c])
                for c in list(selected_configurations) + list(unselected_configurations)
            },
            assignments=extracted_solution["assignments"],
            warnings=extracted_solution["warnings"],
            metadata={
                "selected_configuration_count": len(selected_configurations),
                "unselected_configuration_count": len(unselected_configurations),
                "assignment_count": len(extracted_solution["assignments"]),
            },
        )
    
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
    ) -> ScheduleProposal:
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
            stage_resource_type_quantity,
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
            stage_resource_type_quantity,
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
            stage_resource_type_quantity,
            stage_duration,
            resource_type_active_resources,
            resource_parallel_capacity,
            resource_daily_capacity_minutes,
            self.ts,
        )

        prob = self._solve_problem(objective, constraints)

        extracted_solution = self._extract_solution(
            prob=prob,
            variable_set=variable_set,
            configurations=configurations,
            configuration_margin=configuration_margin,
            configuration_items=configuration_items,
            item_stages=item_stages,
            stage_resource_types=stage_resource_types,
            stage_resource_type_quantity=stage_resource_type_quantity,
            resource_type_active_resources=resource_type_active_resources,
        )

        proposal = self._build_proposal(
            start_date=start_date,
            end_date=end_date,
            configuration_margin=configuration_margin,
            extracted_solution=extracted_solution,
        )

        return proposal
