import math

from configurator.models import Configuration
from production.services.scheduler.data import optimization_dict_creation
from production.services.scheduler.proposal import ScheduleProposal, ScheduledAssignment
from production.services.scheduler.core import ScheduleStrategy
from datetime import date
import pulp
from datetime import datetime, timedelta

class MarginScheduler(ScheduleStrategy):
    def __init__(self, optimization_params):
        self.ts = optimization_params["ts"]
    
    @staticmethod
    def _var_value_is_one(var: pulp.LpVariable, tol: float = 1e-5) -> bool:
        value = pulp.value(var)
        return value is not None and value >= 1 - tol
        
    @staticmethod
    def _stage_required_slots(stage_duration, ts):
        return {
            key: math.ceil(duration / ts)
            for key, duration in stage_duration.items()
        }
    
    @staticmethod
    def _config_stage_suffix_slots(configuration_stages, stage_required_slots):
        """
        For each (config, stage), how many slots are needed from that stage
        to the end of the config's sequence.
        """
        suffix = {}
        for c, stages in configuration_stages.items():
            running = 0
            for stage in reversed(stages):
                running += stage_required_slots[(c, stage)]
                suffix[(c, stage)] = running
        return suffix
    
    @staticmethod
    def _valid_stage_start_positions(
        company_ordered_slots,
        configuration_company,
        configurations,
        configuration_stages,
        stage_suffix_slots,
    ):
        valid_start_positions = {}
        for c in configurations:
            company = configuration_company[c]
            total_slots = len(company_ordered_slots[company])
            for s in configuration_stages[c]:
                needed_for_rest = stage_suffix_slots[(c, s)]
                latest_start = max(0, total_slots - needed_for_rest + 1)
                valid_start_positions[(c, s)] = list(range(latest_start))
        return valid_start_positions

    @staticmethod
    def _covering_start_positions(valid_start_positions, stage_required_slots):
        covering_start_positions = {}
        for key, starts in valid_start_positions.items():
            c, s = key
            needed_slots = stage_required_slots[(c, s)]
            if not starts:
                covering_start_positions[key] = {}
                continue
            max_pos = starts[-1] + needed_slots
            stage_cover = {}
            for pos in range(max_pos):
                stage_cover[pos] = [
                    tau for tau in starts
                    if tau <= pos < tau + needed_slots
                ]
            covering_start_positions[key] = stage_cover
        return covering_start_positions
    
    @staticmethod
    def _resource_symmetry_groups(resource_type_active_resources, resource_parallel_capacity, resource_daily_capacity_minutes):
        groups = {}
        for rt, resources in resource_type_active_resources.items():
            for r in resources:
                key = (
                    rt, r.company_id,
                    resource_parallel_capacity[r],
                    resource_daily_capacity_minutes[r],
                )
                groups.setdefault(key, []).append(r)
        return [(key[0], sorted(resources, key=lambda r: r.pk)) for key, resources in groups.items() if len(resources) > 1]
    
    def _should_use_warm_start(
        self, configurations, configuration_company, company_ordered_slots,
        configuration_stages, stage_resource_types, stage_resource_type_quantity,
        resource_type_active_resources,
    ):
        estimated = 0
        for c in configurations:
            slot_count = len(company_ordered_slots[configuration_company[c]])
            for s in configuration_stages[c]:
                estimated += slot_count
                for rt in stage_resource_types[(c, s)]:
                    qty = stage_resource_type_quantity[(c, s, rt)]
                    rcount = len(resource_type_active_resources[rt])
                    estimated += slot_count * qty * rcount
                    estimated += qty * rcount
        return estimated >= 3000
    
    def _apply_greedy_warm_start(
        self, variable_set, configurations, configuration_margin,
        configuration_company, company_ordered_slots, configuration_stages,
        stage_resource_types, stage_resource_type_quantity,
        stage_required_slots, valid_start_positions, resource_type_active_resources,
    ):
        resource_order = {
            rt: sorted(resources, key=lambda r: r.pk)
            for rt, resources in resource_type_active_resources.items()
        }
        resource_to_type = {
            r: rt for rt, resources in resource_type_active_resources.items()
            for r in resources
        }
        occupied = {r: set() for r in resource_to_type}

        def resource_is_free(resource, slot_range, local_occupied):
            occ = local_occupied[resource]
            return all(pos not in occ for pos in slot_range)

        for var in variable_set["y"].values(): var.setInitialValue(0)
        for var in variable_set["start"].values(): var.setInitialValue(0)
        for var in variable_set["z"].values(): var.setInitialValue(0)
        for var in variable_set["x"].values(): var.setInitialValue(0)

        for c in sorted(configurations, key=lambda c: (-configuration_margin[c], c.pk)):
            company = configuration_company[c]
            local_occupied = {r: set(v) for r, v in occupied.items()}
            chosen_stages = []
            earliest_allowed = 0
            feasible = True

            for s in configuration_stages[c]:
                if not stage_resource_types[(c, s)]:
                    continue
                needed_slots = stage_required_slots[(c, s)]
                chosen = None

                for pos in valid_start_positions[(c, s)]:
                    if pos < earliest_allowed:
                        continue
                    slot_range = range(pos, pos + needed_slots)
                    chosen_resources = {}
                    ok = True

                    for rt in stage_resource_types[(c, s)]:
                        qty = stage_resource_type_quantity[(c, s, rt)]
                        used_this_stage = set()
                        for q in range(qty):
                            resource = next(
                                (r for r in resource_order[rt]
                                 if r not in used_this_stage
                                 and resource_is_free(r, slot_range, local_occupied)),
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
                chosen_stages.append((s, pos, chosen_resources))
                for (rt, q), resource in chosen_resources.items():
                    for slot_pos in range(pos, pos + needed_slots):
                        local_occupied[resource].add(slot_pos)
                earliest_allowed = pos + needed_slots

            if feasible:
                occupied = local_occupied
                variable_set["y"][c].setInitialValue(1)
                for s, pos, chosen_resources in chosen_stages:
                    variable_set["start"][(c, s, pos)].setInitialValue(1)
                    needed_slots = stage_required_slots[(c, s)]
                    for (rt, q), resource in chosen_resources.items():
                        variable_set["z"][(c, s, rt, resource, q)].setInitialValue(1)
                        for slot_pos in range(pos, pos + needed_slots):
                            variable_set["x"][(c, s, slot_pos, resource, q)].setInitialValue(1)

    def _variable_definition(
        self, configurations, configuration_company, company_ordered_slots,
        configuration_stages, stage_resource_types, stage_resource_type_quantity,
        resource_type_active_resources, valid_start_positions,
    ) -> dict:
        x = {
            (c, s, pos, r, q): pulp.LpVariable(
                f"x_{c.pk}_{s}_{pos}_{r.pk}_{q}", cat="Binary"
            )
            for c in configurations
            for s in configuration_stages[c]
            for rt in stage_resource_types[(c, s)]
            for q in range(stage_resource_type_quantity[(c, s, rt)])
            for r in resource_type_active_resources[rt]
            for pos in range(len(company_ordered_slots[configuration_company[c]]))
        }
        y = {c: pulp.LpVariable(f"y_{c.pk}", cat="Binary") for c in configurations}
        start = {
            (c, s, pos): pulp.LpVariable(f"start_{c.pk}_{s}_{pos}", cat="Binary")
            for c in configurations
            for s in configuration_stages[c]
            for pos in valid_start_positions[(c, s)]
        }
        z = {
            (c, s, rt, r, q): pulp.LpVariable(
                f"z_{c.pk}_{s}_{rt[0].pk}_{rt[1]}_{r.pk}_{q}", cat="Binary"
            )
            for c in configurations
            for s in configuration_stages[c]
            for rt in stage_resource_types[(c, s)]
            for q in range(stage_resource_type_quantity[(c, s, rt)])
            for r in resource_type_active_resources[rt]
        }
        return {"x": x, "y": y, "start": start, "z": z}

    def _objective_definition(
        self, variable_set, configuration_margin, configurations,
        configuration_stages, valid_start_positions,
    ):
        margin = pulp.lpSum(
            variable_set["y"][c] * m for c, m in configuration_margin.items()
        )
        EPS = 1e-3
        late_penalty = pulp.lpSum(
            pos * variable_set["start"][(c, s, pos)]
            for c in configurations
            for s in configuration_stages[c]
            for pos in valid_start_positions[(c, s)]
        )
        return margin - EPS * late_penalty

    def _constraint_definition(
        self, variable_set, configurations, configuration_company,
        company_ordered_slots, configuration_stages, stage_resource_types,
        stage_resource_type_quantity, stage_required_slots,
        valid_start_positions, covering_start_positions,
        resource_type_active_resources, resource_parallel_capacity,
        resource_daily_capacity_minutes, ts, resource_slot_load, 
        resource_day_load
    ):
        constraints = {}

        # Each stage starts exactly once if selected
        constraints.update({
            f"Stage_start_once_{c.pk}_{s}": (
                pulp.lpSum(
                    variable_set["start"][(c, s, pos)]
                    for pos in valid_start_positions[(c, s)]
                ) == variable_set["y"][c],
                f"Stage_start_once_{c.pk}_{s}",
            )
            for c in configurations
            for s in configuration_stages[c]
        })

        # Each stage-resource-type copy chooses exactly one concrete resource
        constraints.update({
            f"Choose_one_resource_{c.pk}_{s}_{rt[0].pk}_{rt[1]}_{q}": (
                pulp.lpSum(
                    variable_set["z"][(c, s, rt, r, q)]
                    for r in resource_type_active_resources[rt]
                ) == variable_set["y"][c],
                f"Choose_one_resource_{c.pk}_{s}_{rt[0].pk}_{rt[1]}_{q}",
            )
            for c in configurations
            for s in configuration_stages[c]
            for rt in stage_resource_types[(c, s)]
            for q in range(stage_resource_type_quantity[(c, s, rt)])
        })

        # Stage active profile
        constraints.update({
            f"Stage_active_profile_{c.pk}_{s}_{pos}_{rt[0].pk}_{rt[1]}_{q}": (
                pulp.lpSum(
                    variable_set["x"][(c, s, pos, r, q)]
                    for r in resource_type_active_resources[rt]
                ) == pulp.lpSum(
                    variable_set["start"][(c, s, tau)]
                    for tau in covering_start_positions[(c, s)].get(pos, [])
                    if (c, s, tau) in variable_set["start"]
                ),
                f"Stage_active_profile_{c.pk}_{s}_{pos}_{rt[0].pk}_{rt[1]}_{q}",
            )
            for c in configurations
            for s in configuration_stages[c]
            for rt in stage_resource_types[(c, s)]
            for q in range(stage_resource_type_quantity[(c, s, rt)])
            for pos in range(len(company_ordered_slots[configuration_company[c]]))
        })

        # Use only chosen resource
        constraints.update({
            f"Use_only_chosen_{c.pk}_{s}_{pos}_{r.pk}_{q}": (
                variable_set["x"][(c, s, pos, r, q)]
                <= variable_set["z"][(c, s, rt, r, q)],
                f"Use_only_chosen_{c.pk}_{s}_{pos}_{r.pk}_{q}",
            )
            for c in configurations
            for s in configuration_stages[c]
            for rt in stage_resource_types[(c, s)]
            for q in range(stage_resource_type_quantity[(c, s, rt)])
            for r in resource_type_active_resources[rt]
            for pos in range(len(company_ordered_slots[configuration_company[c]]))
        })

        # Stage sequence
        stages_list = {c: configuration_stages[c] for c in configurations}
        constraints.update({
            f"Stage_sequence_{c.pk}_{prev_s}_{s}": (
                pulp.lpSum(
                    pos * variable_set["start"][(c, s, pos)]
                    for pos in valid_start_positions[(c, s)]
                ) >=
                pulp.lpSum(
                    pos * variable_set["start"][(c, prev_s, pos)]
                    for pos in valid_start_positions[(c, prev_s)]
                ) + stage_required_slots[(c, prev_s)] * variable_set["y"][c],
                f"Stage_sequence_{c.pk}_{prev_s}_{s}",
            )
            for c in configurations
            for idx in range(1, len(stages_list[c]))
            for prev_s in [stages_list[c][idx - 1]]
            for s in [stages_list[c][idx]]
        })

        # Parallel capacity
        constraints.update({
            f"Parallel_capacity_{r.pk}_{pos}": (
                pulp.lpSum(
                    variable_set["x"].get((c, s, pos, r, q), 0)
                    for c in configurations
                    for s in configuration_stages[c]
                    for rt in stage_resource_types[(c, s)]
                    if r in resource_type_active_resources[rt]
                    for q in range(stage_resource_type_quantity[(c, s, rt)])
                ) <= parallel_capacity - resource_slot_load.get((r, pos), 0),
                f"Parallel_capacity_{r.pk}_{pos}",
            )
            for r, parallel_capacity in resource_parallel_capacity.items()
            for pos in range(len(company_ordered_slots[r.company]))
        })

        # Daily capacity
        constraints.update({
            f"Daily_capacity_{r.pk}_{d}": (
                pulp.lpSum(
                    variable_set["x"].get((c, s, pos, r, q), 0)
                    for c in configurations
                    for s in configuration_stages[c]
                    for rt in stage_resource_types[(c, s)]
                    if r in resource_type_active_resources[rt]
                    for q in range(stage_resource_type_quantity[(c, s, rt)])
                    for pos, (slot_day, _) in enumerate(company_ordered_slots[r.company])
                    if slot_day == d
                ) * ts <= daily_cap - resource_day_load.get((r, d), 0),
                f"Daily_capacity_{r.pk}_{d}",
            )
            for r, daily_cap in resource_daily_capacity_minutes.items()
            for d in {slot_day for slot_day, _ in company_ordered_slots[r.company]}
        })

        # Symmetry breaking
        constraints.update(self._resource_symmetry_constraints(
            variable_set=variable_set,
            configurations=configurations,
            configuration_stages=configuration_stages,
            stage_resource_types=stage_resource_types,
            stage_resource_type_quantity=stage_resource_type_quantity,
            company_ordered_slots=company_ordered_slots,
            configuration_company=configuration_company,
            resource_type_active_resources=resource_type_active_resources,
            resource_parallel_capacity=resource_parallel_capacity,
            resource_daily_capacity_minutes=resource_daily_capacity_minutes,
        ))

        return constraints
    
    def _resource_symmetry_constraints(
        self, variable_set, configurations, configuration_stages,
        stage_resource_types, stage_resource_type_quantity,
        company_ordered_slots, configuration_company,
        resource_type_active_resources, resource_parallel_capacity,
        resource_daily_capacity_minutes,
    ):
        constraints = {}
        groups = self._resource_symmetry_groups(
            resource_type_active_resources, resource_parallel_capacity,
            resource_daily_capacity_minutes,
        )
        for group_idx, (rt, group) in enumerate(groups):
            for left, right in zip(group, group[1:]):
                left_load = pulp.lpSum(
                    variable_set["x"].get((c, s, pos, left, q), 0)
                    for c in configurations
                    for s in configuration_stages[c]
                    if rt in stage_resource_types[(c, s)]
                    for q in range(stage_resource_type_quantity[(c, s, rt)])
                    for pos in range(len(company_ordered_slots[configuration_company[c]]))
                )
                right_load = pulp.lpSum(
                    variable_set["x"].get((c, s, pos, right, q), 0)
                    for c in configurations
                    for s in configuration_stages[c]
                    if rt in stage_resource_types[(c, s)]
                    for q in range(stage_resource_type_quantity[(c, s, rt)])
                    for pos in range(len(company_ordered_slots[configuration_company[c]]))
                )
                constraints[f"Symmetry_{group_idx}_{left.pk}_{right.pk}"] = (
                    left_load >= right_load,
                    f"Symmetry_{group_idx}_{left.pk}_{right.pk}",
                )
        return constraints
    
    def _solve_problem(
        self, objective, constraints,
        use_warm_start=False, time_limit=None,
        gap_rel=None, gap_abs=None, max_nodes=None,
    ) -> pulp.LpProblem:
        prob = pulp.LpProblem("Schedule_config_stages", pulp.LpMaximize)
        prob += objective
        for c in constraints.values():
            prob += c
        solver = pulp.PULP_CBC_CMD(
            msg=True, warmStart=use_warm_start, presolve=True, cuts=True,
            timeLimit=time_limit, gapRel=gap_rel, gapAbs=gap_abs, maxNodes=max_nodes,
        )
        prob.solve(solver)
        return prob
    
    def _merge_consecutive_slots(self, slots):
        if not slots:
            return []
        ordered = sorted(slots, key=lambda x: (x[0], x[1]))
        intervals = []
        current_start = datetime.combine(ordered[0][0], ordered[0][1])
        current_end = current_start + timedelta(minutes=self.ts)
        slot_count = 1
        for d, t in ordered[1:]:
            dt = datetime.combine(d, t)
            if dt == current_end:
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
        self, prob, variable_set, configurations, configuration_company,
        company_ordered_slots, configuration_margin, configuration_stages,
        stage_resource_types, stage_resource_type_quantity,
        resource_type_active_resources,
    ) -> dict:
        solver_status = pulp.LpStatus.get(prob.status, str(prob.status))
        is_optimal = solver_status == "Optimal"
        objective_value = pulp.value(prob.objective)

        selected = []
        unselected = []
        assignments = []
        warnings = []

        if solver_status == "Infeasible":
            warnings.append("The optimization problem is infeasible.")
        elif solver_status == "Not Solved":
            warnings.append("The solver did not finish.")

        for c in configurations:
            if self._var_value_is_one(variable_set["y"][c]):
                selected.append(c)
            else:
                unselected.append(c)

        for c in selected:
            company = configuration_company[c]
            for s in configuration_stages[c]:
                if not stage_resource_types[(c, s)]:
                    continue

                seen_resource_q = set()
                for rt in stage_resource_types[(c, s)]:
                    for q in range(stage_resource_type_quantity[(c, s, rt)]):
                        for r in resource_type_active_resources[rt]:
                            key = (r, q)
                            if key in seen_resource_q:
                                continue
                            seen_resource_q.add(key)

                            active_slots = []
                            for pos in range(len(company_ordered_slots[company])):
                                var = variable_set["x"].get((c, s, pos, r, q))
                                if var is not None and self._var_value_is_one(var):
                                    active_slots.append(company_ordered_slots[company][pos])

                            if not active_slots:
                                continue

                            for start_dt, end_dt, sc in self._merge_consecutive_slots(active_slots):
                                assignments.append(ScheduledAssignment(
                                    configuration_id=c.pk,
                                    configuration_name=str(c),
                                    stage_name=s,
                                    resource_id=r.pk,
                                    resource_name=str(r),
                                    start=start_dt,
                                    end=end_dt,
                                    slot_count=sc,
                                    minutes=sc * self.ts,
                                ))

        assignments.sort(key=lambda a: (a.start, a.configuration_id, a.stage_name))
        total_margin = sum(float(configuration_margin[c]) for c in selected)

        return {
            "solver_status": solver_status,
            "is_optimal": is_optimal,
            "objective_value": float(objective_value) if objective_value is not None else None,
            "total_margin": float(total_margin),
            "selected_configurations": selected,
            "unselected_configurations": unselected,
            "assignments": assignments,
            "warnings": warnings,
        }

    def _build_proposal(self, start_date, end_date, configuration_margin, extracted_solution):
        selected = extracted_solution["selected_configurations"]
        unselected = extracted_solution["unselected_configurations"]
        return ScheduleProposal(
            start_date=start_date, end_date=end_date, slot_minutes=self.ts,
            solver_status=extracted_solution["solver_status"],
            is_optimal=extracted_solution["is_optimal"],
            objective_value=extracted_solution["objective_value"],
            total_margin=extracted_solution["total_margin"],
            selected_configuration_ids=[c.pk for c in selected],
            unselected_configuration_ids=[c.pk for c in unselected],
            configuration_margins={
                c.pk: float(configuration_margin[c])
                for c in list(selected) + list(unselected)
            },
            assignments=extracted_solution["assignments"],
            warnings=extracted_solution["warnings"],
            metadata={
                "selected_configuration_count": len(selected),
                "unselected_configuration_count": len(unselected),
                "assignment_count": len(extracted_solution["assignments"]),
            },
        )
    
    def schedule(self, start_date: date, end_date: date, configurations: list[Configuration]) -> ScheduleProposal:
        (
            company_ordered_slots, configuration_company,
            configuration_margin, configuration_stages,
            stage_resource_types, stage_resource_type_quantity, stage_duration,
            resource_type_active_resources, resource_parallel_capacity,
            resource_daily_capacity_minutes, resource_slot_load, 
            resource_day_load
        ) = optimization_dict_creation(start_date, end_date, configurations, self.ts)

        stage_required_slots = self._stage_required_slots(stage_duration, self.ts)
        stage_suffix_slots = self._config_stage_suffix_slots(
            configuration_stages, stage_required_slots,
        )
        valid_start_positions = self._valid_stage_start_positions(
            company_ordered_slots, configuration_company, configurations,
            configuration_stages, stage_suffix_slots,
        )
        covering_start_positions = self._covering_start_positions(
            valid_start_positions, stage_required_slots,
        )
        variable_set = self._variable_definition(
            configurations, configuration_company, company_ordered_slots,
            configuration_stages, stage_resource_types,
            stage_resource_type_quantity, resource_type_active_resources,
            valid_start_positions,
        )
        use_warm_start = self._should_use_warm_start(
            configurations, configuration_company, company_ordered_slots,
            configuration_stages, stage_resource_types,
            stage_resource_type_quantity, resource_type_active_resources,
        )
        if use_warm_start:
            self._apply_greedy_warm_start(
                variable_set, configurations, configuration_margin,
                configuration_company, company_ordered_slots,
                configuration_stages, stage_resource_types,
                stage_resource_type_quantity, stage_required_slots,
                valid_start_positions, resource_type_active_resources,
            )
        objective = self._objective_definition(
            variable_set, configuration_margin, configurations,
            configuration_stages, valid_start_positions,
        )
        constraints = self._constraint_definition(
            variable_set, configurations, configuration_company,
            company_ordered_slots, configuration_stages,
            stage_resource_types, stage_resource_type_quantity,
            stage_required_slots, valid_start_positions,
            covering_start_positions, resource_type_active_resources,
            resource_parallel_capacity, resource_daily_capacity_minutes, self.ts,
            resource_slot_load, resource_day_load
        )
        prob = self._solve_problem(
            objective, constraints, use_warm_start,
            time_limit=120, gap_rel=0.01,
        )

        print("Solver status:", pulp.LpStatus[prob.status])
        print("Objective:", pulp.value(prob.objective))
        for c in configurations:
            print(f"y[{c.pk}] =", pulp.value(variable_set["y"][c]))

        extracted_solution = self._extract_solution(
            prob=prob, variable_set=variable_set,
            configurations=configurations,
            configuration_company=configuration_company,
            company_ordered_slots=company_ordered_slots,
            configuration_margin=configuration_margin,
            configuration_stages=configuration_stages,
            stage_resource_types=stage_resource_types,
            stage_resource_type_quantity=stage_resource_type_quantity,
            resource_type_active_resources=resource_type_active_resources,
        )
        return self._build_proposal(
            start_date=start_date, end_date=end_date,
            configuration_margin=configuration_margin,
            extracted_solution=extracted_solution,
        )
