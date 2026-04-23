import math

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
    def _var_value_is_one(var: pulp.LpVariable, tol: float = 1e-5) -> bool:
        value = pulp.value(var)
        return value is not None and value >= 1 - tol
    
    @staticmethod
    def _slot_indexed_company_slots(possible_days_weekday, company_times_by_weekday, companies):
        """
        Build ordered slot lists per company.

        Returns
        -------
        dict[Company, list[tuple[date, time]]]
            Ordered slots for each company.
        """
        return {
            company: [
                (d, t)
                for d, wd in sorted(possible_days_weekday.items())
                for t in company_times_by_weekday[company].get(wd, [])
            ]
            for company in companies
        }
    
    @staticmethod
    def _stage_required_slots(stage_duration, ts):
        """
        Convert stage duration in minutes to required number of slots.
        """
        return {
            key: math.ceil(duration / ts)
            for key, duration in stage_duration.items()
        }
    
    @staticmethod
    def _item_stage_suffix_slots(configuration_items, item_stages, stage_required_slots):
        """
        For each (item, stage), how many slots are needed from that stage to the end
        of the item's sequence.
        """
        suffix = {}

        for item in {i for items in configuration_items.values() for i in items}:
            running = 0
            for stage in reversed(item_stages[item]):
                running += stage_required_slots[(item, stage)]
                suffix[(item, stage)] = running

        return suffix
    
    @staticmethod
    def _valid_stage_start_positions(
        company_ordered_slots,
        configuration_company,
        configurations,
        configuration_items,
        item_stages,
        stage_suffix_slots,
    ):
        """
        Stage starts are bounded so that the stage itself and all later stages can fit.
        This is a big pruning win.
        """
        valid_start_positions = {}

        for c in configurations:
            company = configuration_company[c]
            total_slots = len(company_ordered_slots[company])

            for i in configuration_items[c]:
                for s in item_stages[i]:
                    needed_for_rest_of_item = stage_suffix_slots[(i, s)]
                    latest_start = max(0, total_slots - needed_for_rest_of_item + 1)
                    valid_start_positions[(c, i, s)] = list(range(latest_start))

        return valid_start_positions

    @staticmethod
    def _covering_start_positions(valid_start_positions, stage_required_slots):
        """
        For each stage and slot position p, returns the start positions that make the
        stage active at p.

        If a stage starts at tau and needs n slots, it is active at positions:
            tau, tau+1, ..., tau+n-1
        """
        covering_start_positions = {}

        for key, starts in valid_start_positions.items():
            i, s = key[1], key[2]
            needed_slots = stage_required_slots[(i, s)]

            if not starts:
                covering_start_positions[key] = {}
                continue

            max_pos = starts[-1] + needed_slots
            stage_cover = {}

            for pos in range(max_pos):
                stage_cover[pos] = [
                    tau
                    for tau in starts
                    if tau <= pos < tau + needed_slots
                ]

            covering_start_positions[key] = stage_cover

        return covering_start_positions
    
    @staticmethod
    def _resource_symmetry_groups(resource_type_active_resources, resource_parallel_capacity, resource_daily_capacity_minutes):
        """
        Group interchangeable resources. Only resources with the same type and the
        same capacities are treated as symmetric.
        """
        groups = {}

        for rt, resources in resource_type_active_resources.items():
            for r in resources:
                key = (
                    rt,
                    r.company_id,
                    resource_parallel_capacity[r],
                    resource_daily_capacity_minutes[r],
                )
                groups.setdefault(key, []).append(r)

        return [sorted(resources, key=lambda r: r.pk) for resources in groups.values() if len(resources) > 1]
    
    def _should_use_warm_start(
        self,
        configurations,
        configuration_company,
        company_ordered_slots,
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        resource_type_active_resources,
    ):
        estimated = 0

        for c in configurations:
            slot_count = len(company_ordered_slots[configuration_company[c]])

            for i in configuration_items[c]:
                for s in item_stages[i]:
                    estimated += slot_count  # start variables

                    for rt in stage_resource_types[(i, s)]:
                        qty = stage_resource_type_quantity[(i, s, rt)]
                        rcount = len(resource_type_active_resources[rt])

                        estimated += slot_count * qty * rcount  # x variables
                        estimated += qty * rcount               # z variables

        return estimated >= 3000
    
    def _apply_greedy_warm_start(
        self,
        variable_set,
        configurations,
        configuration_margin,
        configuration_company,
        company_ordered_slots,
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        stage_required_slots,
        valid_start_positions,
        resource_type_active_resources,
    ):
        """
        Build a feasible greedy start:
        - sort configurations by margin descending
        - place each stage at the earliest feasible position
        - choose the first free interchangeable resource(s)
        """
        resource_to_type = {
            r: rt
            for rt, resources in resource_type_active_resources.items()
            for r in resources
        }
        resource_order = {
            rt: sorted(resources, key=lambda r: r.pk)
            for rt, resources in resource_type_active_resources.items()
        }

        occupied = {r: set() for r in resource_to_type}

        def resource_is_free(resource, slot_range, local_occupied):
            occ = local_occupied[resource]
            return all(pos not in occ for pos in slot_range)

        # Default everything to 0
        for var in variable_set["y"].values():
            var.setInitialValue(0)
        for var in variable_set["start"].values():
            var.setInitialValue(0)
        for var in variable_set["z"].values():
            var.setInitialValue(0)
        for var in variable_set["x"].values():
            var.setInitialValue(0)

        for c in sorted(configurations, key=lambda c: (-configuration_margin[c], c.pk)):
            company = configuration_company[c]
            slots = company_ordered_slots[company]
            local_occupied = {r: set(v) for r, v in occupied.items()}

            chosen_stages = []
            earliest_allowed = 0
            feasible = True

            for i in configuration_items[c]:
                for s in item_stages[i]:
                    if not stage_resource_types[(i, s)]:
                        continue

                    needed_slots = stage_required_slots[(i, s)]
                    chosen = None

                    for pos in valid_start_positions[(c, i, s)]:
                        if pos < earliest_allowed:
                            continue

                        slot_range = range(pos, pos + needed_slots)
                        chosen_resources = {}
                        ok = True

                        for rt in stage_resource_types[(i, s)]:
                            qty = stage_resource_type_quantity[(i, s, rt)]
                            used_this_stage = set()

                            for q in range(qty):
                                resource = next(
                                    (
                                        r
                                        for r in resource_order[rt]
                                        if r not in used_this_stage
                                        and resource_is_free(r, slot_range, local_occupied)
                                    ),
                                    None,
                                )
                                if resource is None:
                                    ok = False
                                    break

                                used_this_stage.add(resource)
                                chosen_resources[(rt, q)] = resource

                            if not ok:
                                break

                        if ok:
                            chosen = (pos, chosen_resources)
                            break

                    if chosen is None:
                        feasible = False
                        break

                    pos, chosen_resources = chosen
                    chosen_stages.append((i, s, pos, chosen_resources))

                    # commit occupancy tentatively
                    for (rt, q), resource in chosen_resources.items():
                        for slot_pos in range(pos, pos + needed_slots):
                            local_occupied[resource].add(slot_pos)

                    earliest_allowed = pos + needed_slots

                if not feasible:
                    break

            if feasible:
                occupied = local_occupied
                variable_set["y"][c].setInitialValue(1)

                for i, s, pos, chosen_resources in chosen_stages:
                    variable_set["start"][(c, i, s, pos)].setInitialValue(1)
                    needed_slots = stage_required_slots[(i, s)]

                    for (rt, q), resource in chosen_resources.items():
                        variable_set["z"][(c, i, s, rt, resource, q)].setInitialValue(1)
                        for slot_pos in range(pos, pos + needed_slots):
                            variable_set["x"][(c, i, s, slot_pos, resource, q)].setInitialValue(1)

    def _variable_definition(
        self,
        configurations,
        configuration_company,
        company_ordered_slots,
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        resource_type_active_resources,
        valid_start_positions,
    ) -> dict:
        """
        Variables
        ---------
        x[c,i,s,pos,r,q]
            1 if at slot position pos, resource r is occupied by stage s copy q
        
        y[c]
            1 if configuration c is selected

        start[c,i,s,pos]
            1 if stage s of item i in configuration c starts at slot position pos

        z[c,i,s,rt,r,q]
            1 if for stage s, resource copy q of resource type rt is assigned to resource r
        """

        x = {
            (c, i, s, pos, r, q): pulp.LpVariable(
                f"x_{c.pk}_{i.pk}_{s}_{pos}_{r.pk}_{q}", cat="Binary"
            )
            for c in configurations
            for i in configuration_items[c]
            for s in item_stages[i]
            for rt in stage_resource_types[(i, s)]
            for q in range(stage_resource_type_quantity[(i, s, rt)])
            for r in resource_type_active_resources[rt]
            for pos in range(len(company_ordered_slots[configuration_company[c]]))
        }

        y = {c: pulp.LpVariable(f"y_{c.pk}", cat="Binary") for c in configurations}

        start = {
            (c, i, s, pos): pulp.LpVariable(
                f"start_{c.pk}_{i.pk}_{s}_{pos}",
                cat="Binary",
            )
            for c in configurations
            for i in configuration_items[c]
            for s in item_stages[i]
            for pos in valid_start_positions[(c, i, s)]
        }

        z = {
            (c, i, s, rt, r, q): pulp.LpVariable(
                f"z_{c.pk}_{i.pk}_{s}_{rt[0].pk}_{rt[1]}_{r.pk}_{q}",
                cat="Binary",
            )
            for c in configurations
            for i in configuration_items[c]
            for s in item_stages[i]
            for rt in stage_resource_types[(i, s)]
            for q in range(stage_resource_type_quantity[(i, s, rt)])
            for r in resource_type_active_resources[rt]
        }

        variable_set = {"x": x, "y": y, "start": start, "z": z}

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
        configuration_company,
        company_ordered_slots,
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        stage_required_slots,
        valid_start_positions,
        covering_start_positions,
        resource_type_active_resources,
        resource_parallel_capacity,
        resource_daily_capacity_minutes,
        ts,
    ):
        constraints = {}

        resource_to_type = {
            r: rt
            for rt, resources in resource_type_active_resources.items()
            for r in resources
        }

        # Each stage starts exactly once if the configuration is selected
        constraints.update(
            {
                f"Stage_start_once_{c.pk}_{i.pk}_{s}": (
                    pulp.lpSum(
                        variable_set["start"][(c, i, s, pos)]
                        for pos in valid_start_positions[(c, i, s)]
                    )
                    == variable_set["y"][c],
                    f"Stage_start_once_{c.pk}_{i.pk}_{s}",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
            }
        )

        # Each stage-resource-type copy chooses exactly one concrete resource if selected
        constraints.update(
            {
                f"Choose_one_resource_{c.pk}_{i.pk}_{s}_{rt[0].pk}_{rt[1]}_{q}": (
                    pulp.lpSum(
                        variable_set["z"][(c, i, s, rt, r, q)]
                        for r in resource_type_active_resources[rt]
                    )
                    == variable_set["y"][c],
                    f"Choose_one_resource_{c.pk}_{i.pk}_{s}_{rt[0].pk}_{rt[1]}_{q}",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for rt in stage_resource_types[(i, s)]
                for q in range(stage_resource_type_quantity[(i, s, rt)])
            }
        )

        # For each resource type copy q, exactly one concrete resource must be occupied
        # whenever the stage is active at that slot (start and consecutive slots to meet duration)
        constraints.update(
            {
                f"Stage_active_profile_{c.pk}_{i.pk}_{s}_{pos}_{rt[0].pk}_{rt[1]}_{q}": (
                    pulp.lpSum(
                        variable_set["x"][(c, i, s, pos, r, q)]
                        for r in resource_type_active_resources[rt]
                    )
                    == pulp.lpSum(
                        variable_set["start"][(c, i, s, tau)]
                        for tau in covering_start_positions[(c, i, s)].get(pos, [])
                        if (c, i, s, tau) in variable_set["start"]
                    ),
                    f"Stage_active_profile_{c.pk}_{i.pk}_{s}_{pos}_{rt[0].pk}_{rt[1]}_{q}",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for rt in stage_resource_types[(i, s)]
                for q in range(stage_resource_type_quantity[(i, s, rt)])
                for pos in range(len(company_ordered_slots[configuration_company[c]]))
            }
        )

        # A slot can only use a resource if that resource was chosen for that stage/resource copy
        constraints.update(
            {
                f"Use_only_chosen_resource_{c.pk}_{i.pk}_{s}_{pos}_{r.pk}_{q}": (
                    variable_set["x"][(c, i, s, pos, r, q)]
                    <= variable_set["z"][(c, i, s, resource_to_type[r], r, q)],
                    f"Use_only_chosen_resource_{c.pk}_{i.pk}_{s}_{pos}_{r.pk}_{q}",
                )
                for c in configurations
                for i in configuration_items[c]
                for s in item_stages[i]
                for rt in stage_resource_types[(i, s)]
                for q in range(stage_resource_type_quantity[(i, s, rt)])
                for r in resource_type_active_resources[rt]
                for pos in range(len(company_ordered_slots[configuration_company[c]]))
            }
        )

        # Stage sequence:
        # if y=1, start(next) >= start(prev) + required_slots(prev)
        constraints.update(
            {
                f"Stage_sequence_{c.pk}_{i.pk}_{prev_s}_{s}": (
                    pulp.lpSum(
                        pos * variable_set["start"][(c, i, s, pos)]
                        for pos in valid_start_positions[(c, i, s)]
                    )
                    >=
                    pulp.lpSum(
                        pos * variable_set["start"][(c, i, prev_s, pos)]
                        for pos in valid_start_positions[(c, i, prev_s)]
                    )
                    + stage_required_slots[(i, prev_s)] * variable_set["y"][c],
                    f"Stage_sequence_{c.pk}_{i.pk}_{prev_s}_{s}",
                )
                for c in configurations
                for i in configuration_items[c]
                for stage_idx in range(1, len(item_stages[i]))
                for prev_s in [item_stages[i][stage_idx - 1]]
                for s in [item_stages[i][stage_idx]]
            }
        )

        # Parallel capacity:
        # a resource cannot process more simultaneous stage copies than its parallel_capacity
        constraints.update(
            {
                f"Parallel_capacity_{r.pk}_{pos}": (
                    pulp.lpSum(
                        variable_set["x"].get((c, i, s, pos, r, q), 0)
                        for c in configurations
                        for i in configuration_items[c]
                        for s in item_stages[i]
                        if resource_to_type[r] in stage_resource_types[(i, s)]
                        for q in range(
                            stage_resource_type_quantity[(i, s, resource_to_type[r])]
                        )
                    )
                    <= parallel_capacity,
                    f"Parallel_capacity_{r.pk}_{pos}",
                )
                for r, parallel_capacity in resource_parallel_capacity.items()
                for pos in range(len(company_ordered_slots[r.company]))
            }
        )

        # Daily capacity in minutes
        constraints.update(
            {
                f"Daily_capacity_minutes_{r.pk}_{d}": (
                    pulp.lpSum(
                        variable_set["x"].get((c, i, s, pos, r, q), 0)
                        for c in configurations
                        for i in configuration_items[c]
                        for s in item_stages[i]
                        if resource_to_type[r] in stage_resource_types[(i, s)]
                        for q in range(
                            stage_resource_type_quantity[(i, s, resource_to_type[r])]
                        )
                        for pos, (slot_day, _) in enumerate(company_ordered_slots[r.company])
                        if slot_day == d
                    ) * ts
                    <= daily_capacity_minutes,
                    f"Daily_capacity_minutes_{r.pk}_{d}",
                )
                for r, daily_capacity_minutes in resource_daily_capacity_minutes.items()
                for d in {
                    slot_day
                    for slot_day, _ in company_ordered_slots[r.company]
                }
            }
        )

        constraints.update(
            self._resource_symmetry_constraints(
                variable_set=variable_set,
                configurations=configurations,
                configuration_items=configuration_items,
                item_stages=item_stages,
                stage_resource_types=stage_resource_types,
                stage_resource_type_quantity=stage_resource_type_quantity,
                company_ordered_slots=company_ordered_slots,
                configuration_company=configuration_company,
                resource_type_active_resources=resource_type_active_resources,
                resource_parallel_capacity=resource_parallel_capacity,
                resource_daily_capacity_minutes=resource_daily_capacity_minutes,
            )
        )

        return constraints
    
    def _resource_symmetry_constraints(
        self,
        variable_set,
        configurations,
        configuration_items,
        item_stages,
        stage_resource_types,
        stage_resource_type_quantity,
        company_ordered_slots,
        configuration_company,
        resource_type_active_resources,
        resource_parallel_capacity,
        resource_daily_capacity_minutes,
    ):
        constraints = {}
        groups = self._resource_symmetry_groups(
            resource_type_active_resources,
            resource_parallel_capacity,
            resource_daily_capacity_minutes,
        )

        resource_to_type = {
            r: rt
            for rt, resources in resource_type_active_resources.items()
            for r in resources
        }

        for group_idx, group in enumerate(groups):
            for left, right in zip(group, group[1:]):
                left_rt = resource_to_type[left]

                left_load = pulp.lpSum(
                    variable_set["x"].get((c, i, s, pos, left, q), 0)
                    for c in configurations
                    for i in configuration_items[c]
                    for s in item_stages[i]
                    if left_rt in stage_resource_types[(i, s)]
                    for q in range(stage_resource_type_quantity[(i, s, left_rt)])
                    for pos in range(len(company_ordered_slots[configuration_company[c]]))
                )

                right_load = pulp.lpSum(
                    variable_set["x"].get((c, i, s, pos, right, q), 0)
                    for c in configurations
                    for i in configuration_items[c]
                    for s in item_stages[i]
                    if left_rt in stage_resource_types[(i, s)]
                    for q in range(stage_resource_type_quantity[(i, s, left_rt)])
                    for pos in range(len(company_ordered_slots[configuration_company[c]]))
                )

                constraints[f"Symmetry_resource_load_{group_idx}_{left.pk}_{right.pk}"] = (
                    left_load >= right_load,
                    f"Symmetry_resource_load_{group_idx}_{left.pk}_{right.pk}",
                )

        return constraints
    
    def _solve_problem(
        self,
        objective: pulp.LpAffineExpression,
        constraints: dict,
        use_warm_start: bool = False,
    ) -> pulp.LpProblem:
        prob = pulp.LpProblem("Schedule configuration stages to resources", pulp.LpMaximize)
        prob += objective

        for c in constraints.values():
            prob += c

        solver = pulp.PULP_CBC_CMD(
            msg=True,
            warmStart=use_warm_start,
            presolve=True,
            cuts=True,
        )
        prob.solve(solver)
        return prob
    
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
        configuration_company,
        company_ordered_slots,
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

        # Read x variables back into date/time slots for display
        for c in selected_configurations:
            company = configuration_company[c]

            for i in configuration_items[c]:
                for s in item_stages[i]:
                    if not stage_resource_types[(i, s)]:
                        continue

                    seen_resource_q_pairs = set()

                    for rt in stage_resource_types[(i, s)]:
                        for q in range(stage_resource_type_quantity[(i, s, rt)]):
                            for r in resource_type_active_resources[rt]:
                                key = (r, q)
                                if key in seen_resource_q_pairs:
                                    continue
                                seen_resource_q_pairs.add(key)

                                active_slots = []
                                for pos in range(len(company_ordered_slots[company])):
                                    var = variable_set["x"].get((c, i, s, pos, r, q))
                                    if var is not None and self._var_value_is_one(var):
                                        active_slots.append(company_ordered_slots[company][pos])

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

        company_ordered_slots = self._slot_indexed_company_slots(
            possible_days_weekday,
            company_times_by_weekday,
            {configuration_company[c] for c in configurations},
        )

        stage_required_slots = self._stage_required_slots(stage_duration, self.ts)
        stage_suffix_slots = self._item_stage_suffix_slots(
            configuration_items,
            item_stages,
            stage_required_slots,
        )

        valid_start_positions = self._valid_stage_start_positions(
            company_ordered_slots,
            configuration_company,
            configurations,
            configuration_items,
            item_stages,
            stage_suffix_slots,
        )

        covering_start_positions = self._covering_start_positions(
            valid_start_positions,
            stage_required_slots,
        )

        variable_set = self._variable_definition(
            configurations,
            configuration_company,
            company_ordered_slots,
            configuration_items,
            item_stages,
            stage_resource_types,
            stage_resource_type_quantity,
            resource_type_active_resources,
            valid_start_positions,
        )

        use_warm_start = self._should_use_warm_start(
            configurations,
            configuration_company,
            company_ordered_slots,
            configuration_items,
            item_stages,
            stage_resource_types,
            stage_resource_type_quantity,
            resource_type_active_resources,
        )

        if use_warm_start:
            self._apply_greedy_warm_start(
                variable_set,
                configurations,
                configuration_margin,
                configuration_company,
                company_ordered_slots,
                configuration_items,
                item_stages,
                stage_resource_types,
                stage_resource_type_quantity,
                stage_required_slots,
                valid_start_positions,
                resource_type_active_resources,
            )

        objective = self._objective_definition(variable_set, configuration_margin)

        constraints = self._constraint_definition(
            variable_set,
            configurations,
            configuration_company,
            company_ordered_slots,
            configuration_items,
            item_stages,
            stage_resource_types,
            stage_resource_type_quantity,
            stage_required_slots,
            valid_start_positions,
            covering_start_positions,
            resource_type_active_resources,
            resource_parallel_capacity,
            resource_daily_capacity_minutes,
            self.ts,
        )

        prob = self._solve_problem(objective, constraints, use_warm_start)

        print("Solver status:", pulp.LpStatus[prob.status])
        print("Objective:", pulp.value(prob.objective))

        for c in configurations:
            print(f"y[{c.pk}] =", pulp.value(variable_set["y"][c]))

        extracted_solution = self._extract_solution(
            prob=prob,
            variable_set=variable_set,
            configurations=configurations,
            configuration_company=configuration_company,
            company_ordered_slots=company_ordered_slots,
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
