import math
import os
from collections import defaultdict

from configurator.models import Configuration
from production.services.scheduler.data import optimization_dict_creation
from production.services.scheduler.pooling import pool_interchangeable_resources
from production.services.scheduler.proposal import ScheduleProposal, ScheduledAssignment
from production.services.scheduler.core import ScheduleStrategy
from production.services.scheduler import reasons
from datetime import date
import pulp
from datetime import datetime, timedelta

DEFAULT_TIME_LIMIT_SECONDS = 60
DEFAULT_GAP_REL = 0.05


class MarginScheduler(ScheduleStrategy):
    def __init__(self, optimization_params):
        self.ts = optimization_params["ts"]
        self.time_limit = optimization_params.get("time_limit") or DEFAULT_TIME_LIMIT_SECONDS
        self.gap_rel = optimization_params.get("gap_rel") or DEFAULT_GAP_REL
    
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
    def _config_stage_prefix_slots(configuration_stages, stage_required_slots):
        """
        For each (config, stage), how many slots the stages BEFORE it need:
        the stage can never be active earlier than that position.
        """
        prefix = {}
        for c, stages in configuration_stages.items():
            running = 0
            for stage in stages:
                prefix[(c, stage)] = running
                running += stage_required_slots[(c, stage)]
        return prefix

    @staticmethod
    def _valid_stage_start_positions(
        company_ordered_slots,
        configuration_company,
        configurations,
        configuration_stages,
        stage_suffix_slots,
        stage_prefix_slots,
    ):
        valid_start_positions = {}
        for c in configurations:
            company = configuration_company[c]
            total_slots = len(company_ordered_slots[company])
            for s in configuration_stages[c]:
                earliest_start = stage_prefix_slots[(c, s)]
                needed_for_rest = stage_suffix_slots[(c, s)]
                latest_start = total_slots - needed_for_rest + 1
                valid_start_positions[(c, s)] = list(
                    range(earliest_start, max(earliest_start, latest_start))
                )
        return valid_start_positions

    @staticmethod
    def _stage_position_windows(
        company_ordered_slots, configuration_company, configurations,
        configuration_stages, stage_required_slots,
        stage_prefix_slots, stage_suffix_slots,
    ):
        """
        For each (config, stage), the range of slot positions the stage can
        possibly occupy: not before its predecessors fit, not so late that
        the following stages no longer fit. x variables and their
        constraints only exist inside this window.
        """
        windows = {}
        for c in configurations:
            total_slots = len(company_ordered_slots[configuration_company[c]])
            for s in configuration_stages[c]:
                first = stage_prefix_slots[(c, s)]
                slots_after = stage_suffix_slots[(c, s)] - stage_required_slots[(c, s)]
                windows[(c, s)] = range(first, max(first, total_slots - slots_after))
        return windows

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
    
    def _structural_unschedulable_reasons(
        self, configurations, configuration_company, company_ordered_slots,
        configuration_stages, stage_resource_types, stage_resource_type_quantity,
        stage_suffix_slots, valid_start_positions,
        resource_type_active_resources, resource_parallel_capacity,
    ) -> dict[int, list[dict]]:
        """
        Detects, per configuration, conditions that make it impossible to
        schedule regardless of the other configurations competing for
        capacity. Returns {configuration pk: [reason dict, ...]}.
        """
        result = {}

        for c in configurations:
            c_reasons = []
            stages = configuration_stages[c]
            company = configuration_company[c]
            total_slots = len(company_ordered_slots.get(company, []))

            if not stages:
                c_reasons.append({"code": reasons.NO_SCHEDULABLE_STAGES})
            elif not total_slots:
                c_reasons.append({
                    "code": reasons.NO_COMPANY_TIME_SLOTS,
                    "company": str(company),
                })
            elif not valid_start_positions[(c, stages[0])]:
                # Suffix slots decrease along the sequence, so if any stage
                # has no valid start, the first one doesn't either.
                needed = stage_suffix_slots[(c, stages[0])]
                c_reasons.append({
                    "code": reasons.HORIZON_TOO_SHORT,
                    "needed_slots": needed,
                    "needed_minutes": needed * self.ts,
                    "available_slots": total_slots,
                })

            for s in stages:
                for rt in stage_resource_types[(c, s)]:
                    kind = rt[0].model  # "machine" or "employee"
                    active = resource_type_active_resources.get(rt, [])
                    if not active:
                        c_reasons.append({
                            "code": reasons.NO_ACTIVE_RESOURCES,
                            "stage": s,
                            "kind": kind,
                        })
                        continue

                    required = stage_resource_type_quantity[(c, s, rt)]
                    capacity = sum(
                        resource_parallel_capacity.get(r, 0) for r in active
                    )
                    if required > capacity:
                        c_reasons.append({
                            "code": reasons.INSUFFICIENT_RESOURCE_CAPACITY,
                            "stage": s,
                            "kind": kind,
                            "required": required,
                            "available": capacity,
                        })

            if c_reasons:
                result[c.pk] = c_reasons

        return result

    def _strategy_unselected_reason(self, configuration, margin) -> dict | None:
        """
        Strategy-specific reason why a schedulable configuration would be
        left out. Overridden by subclasses with other objectives.
        """
        if margin <= 0:
            return {"code": reasons.NON_POSITIVE_MARGIN, "margin": margin}
        return None

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
        for var in variable_set["running"].values(): var.setInitialValue(0)

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
                            if (resource, slot_pos) in variable_set["running"]:
                                variable_set["running"][(resource, slot_pos)].setInitialValue(1)

    def _variable_definition(
        self, configurations, configuration_company, company_ordered_slots,
        configuration_stages, stage_resource_types, stage_resource_type_quantity,
        resource_type_active_resources, valid_start_positions,
        stage_position_windows,
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
            for pos in stage_position_windows[(c, s)]
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
        running = {
            (r, pos): pulp.LpVariable(f"running_{r.pk}_{pos}", cat="Binary")
            for r in {
                resource
                for resources in resource_type_active_resources.values()
                for resource in resources
            }
            if r.counts_running_time_once
            for pos in range(len(company_ordered_slots[r.company]))
        }
        return {"x": x, "y": y, "start": start, "z": z, "running": running}

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
        resource_day_load, stage_position_windows, resource_slot_covered_minutes=None,
    ):
        constraints = {}
        resource_slot_covered_minutes = resource_slot_covered_minutes or {}

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
            for pos in stage_position_windows[(c, s)]
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
            for pos in stage_position_windows[(c, s)]
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

        # One unattended machine uses daily running time once per occupied slot,
        # regardless of how many jobs share that slot.
        for (resource, pos), running in variable_set["running"].items():
            slot_jobs = pulp.lpSum(
                variable_set["x"].get((c, s, pos, resource, q), 0)
                for c in configurations
                for s in configuration_stages[c]
                for rt in stage_resource_types[(c, s)]
                if resource in resource_type_active_resources[rt]
                for q in range(stage_resource_type_quantity[(c, s, rt)])
            )
            constraints[f"Running_if_assigned_{resource.pk}_{pos}"] = (
                slot_jobs <= resource_parallel_capacity[resource] * running,
                f"Running_if_assigned_{resource.pk}_{pos}",
            )
            constraints[f"Running_only_if_assigned_{resource.pk}_{pos}"] = (
                running <= slot_jobs,
                f"Running_only_if_assigned_{resource.pk}_{pos}",
            )

        # Daily capacity
        for resource, daily_cap in resource_daily_capacity_minutes.items():
            slots = company_ordered_slots[resource.company]
            for day in {slot_day for slot_day, _ in slots}:
                if resource.counts_running_time_once:
                    member = resource.members[0]
                    daily_minutes = pulp.lpSum(
                        variable_set["running"][(resource, pos)] * (
                            ts - resource_slot_covered_minutes.get((member, pos), 0)
                        )
                        for pos, (slot_day, _) in enumerate(slots)
                        if slot_day == day
                    )
                else:
                    daily_minutes = pulp.lpSum(
                        variable_set["x"].get((c, s, pos, resource, q), 0)
                        for c in configurations
                        for s in configuration_stages[c]
                        for rt in stage_resource_types[(c, s)]
                        if resource in resource_type_active_resources[rt]
                        for q in range(stage_resource_type_quantity[(c, s, rt)])
                        for pos, (slot_day, _) in enumerate(slots)
                        if slot_day == day
                    ) * ts
                name = f"Daily_capacity_{resource.pk}_{day}"
                constraints[name] = (
                    daily_minutes <= daily_cap - resource_day_load.get((resource, day), 0),
                    name,
                )

        return constraints

    @staticmethod
    def _make_solver(use_warm_start, time_limit, gap_rel, gap_abs, max_nodes):
        try:
            solver = pulp.HiGHS(
                msg=False, timeLimit=time_limit, gapRel=gap_rel, gapAbs=gap_abs,
            )
            if solver.available():
                return solver
        except Exception:
            pass
        return pulp.PULP_CBC_CMD(
            msg=False, warmStart=use_warm_start, presolve=True, cuts=True,
            timeLimit=time_limit, gapRel=gap_rel, gapAbs=gap_abs,
            maxNodes=max_nodes, threads=max(1, (os.cpu_count() or 2) - 1),
        )

    def _solve_problem(
        self, objective, constraints,
        use_warm_start=False, time_limit=None,
        gap_rel=None, gap_abs=None, max_nodes=None,
    ) -> pulp.LpProblem:
        prob = pulp.LpProblem("Schedule_config_stages", pulp.LpMaximize)
        prob += objective
        for c in constraints.values():
            prob += c
        solver = self._make_solver(
            use_warm_start, time_limit, gap_rel, gap_abs, max_nodes,
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

    def _assign_pool_members(
        self, pool_usages, company_ordered_slots,
        member_parallel_capacity, member_slot_load,
    ):
        """
        Maps each pooled stage occupation back to concrete resources.

        The model guarantees the pool as a whole has capacity, so a
        first-fit over members (earliest usages first) almost always finds a
        single member for the entire interval; when pre-existing sessions
        fragment availability, the stage is split across members and a
        warning is emitted.
        """
        used = defaultdict(int)
        assignments = []
        warnings = []

        def has_room(member, pos):
            capacity = member_parallel_capacity.get(member, 0)
            taken = member_slot_load.get((member, pos), 0) + used[(member, pos)]
            return capacity - taken > 0

        for usage in sorted(pool_usages, key=lambda u: u["positions"][0]):
            pool = usage["pool"]
            positions = usage["positions"]

            member = next(
                (m for m in pool.members
                 if all(has_room(m, pos) for pos in positions)),
                None,
            )
            if member is not None:
                chunks = [(member, positions)]
            else:
                # No single member covers the whole interval: split it at
                # availability boundaries.
                chunks = []
                current_member, current_chunk = None, []
                for pos in positions:
                    if current_member is not None and has_room(current_member, pos):
                        current_chunk.append(pos)
                        continue
                    if current_chunk:
                        chunks.append((current_member, current_chunk))
                    current_member = next(
                        (m for m in pool.members if has_room(m, pos)),
                        pool.members[0],
                    )
                    current_chunk = [pos]
                chunks.append((current_member, current_chunk))
                warnings.append(
                    f"Stage '{usage['stage']}' of {usage['configuration']} was "
                    f"split across {len(chunks)} resources of the same type "
                    "because no single one was free for the whole interval."
                )

            for member, chunk in chunks:
                for pos in chunk:
                    used[(member, pos)] += 1
                slots = [
                    company_ordered_slots[usage["company"]][pos] for pos in chunk
                ]
                for start_dt, end_dt, sc in self._merge_consecutive_slots(slots):
                    assignments.append(ScheduledAssignment(
                        configuration_id=usage["configuration"].pk,
                        configuration_name=str(usage["configuration"]),
                        stage_name=usage["stage"],
                        resource_id=member.pk,
                        resource_name=str(member),
                        start=start_dt,
                        end=end_dt,
                        slot_count=sc,
                        minutes=sc * self.ts,
                    ))

        return assignments, warnings

    def _extract_solution(
        self, prob, variable_set, configurations, configuration_company,
        company_ordered_slots, configuration_margin, configuration_stages,
        stage_resource_types, stage_resource_type_quantity,
        resource_type_active_resources, member_parallel_capacity,
        member_slot_load, structural_reasons=None,
    ) -> dict:
        structural_reasons = structural_reasons or {}
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
            # A structurally unschedulable configuration can never produce
            # assignments, even if the solver left its y variable free
            # (e.g. a configuration without schedulable stages).
            if c.pk in structural_reasons:
                unselected.append(c)
            elif self._var_value_is_one(variable_set["y"][c]):
                selected.append(c)
            else:
                unselected.append(c)

        unselected_reasons = {}
        for c in unselected:
            c_reasons = list(structural_reasons.get(c.pk, []))
            if not c_reasons:
                strategy_reason = self._strategy_unselected_reason(
                    c, configuration_margin[c],
                )
                if strategy_reason is not None:
                    c_reasons.append(strategy_reason)
            if not c_reasons:
                if not is_optimal:
                    c_reasons.append({
                        "code": reasons.SOLVER_INCOMPLETE,
                        "status": solver_status,
                    })
                else:
                    c_reasons.append({"code": reasons.CAPACITY_COMPETITION})
            unselected_reasons[c.pk] = c_reasons

        pool_usages = []
        for c in selected:
            company = configuration_company[c]
            for s in configuration_stages[c]:
                if not stage_resource_types[(c, s)]:
                    continue

                seen_pool_q = set()
                for rt in stage_resource_types[(c, s)]:
                    for q in range(stage_resource_type_quantity[(c, s, rt)]):
                        for pool in resource_type_active_resources[rt]:
                            key = (pool, q)
                            if key in seen_pool_q:
                                continue
                            seen_pool_q.add(key)

                            positions = []
                            for pos in range(len(company_ordered_slots[company])):
                                var = variable_set["x"].get((c, s, pos, pool, q))
                                if var is not None and self._var_value_is_one(var):
                                    positions.append(pos)

                            if positions:
                                pool_usages.append({
                                    "configuration": c,
                                    "company": company,
                                    "stage": s,
                                    "pool": pool,
                                    "positions": positions,
                                })

        assignments, split_warnings = self._assign_pool_members(
            pool_usages, company_ordered_slots,
            member_parallel_capacity, member_slot_load,
        )
        warnings.extend(split_warnings)

        assignments.sort(key=lambda a: (a.start, a.configuration_id, a.stage_name))
        total_margin = sum(float(configuration_margin[c]) for c in selected)

        return {
            "solver_status": solver_status,
            "is_optimal": is_optimal,
            "objective_value": float(objective_value) if objective_value is not None else None,
            "total_margin": float(total_margin),
            "selected_configurations": selected,
            "unselected_configurations": unselected,
            "unselected_reasons": unselected_reasons,
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
            unselected_reasons=extracted_solution["unselected_reasons"],
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
            resource_day_load, resource_slot_covered_minutes,
        ) = optimization_dict_creation(start_date, end_date, configurations, self.ts)

        stage_required_slots = self._stage_required_slots(stage_duration, self.ts)
        stage_suffix_slots = self._config_stage_suffix_slots(
            configuration_stages, stage_required_slots,
        )
        stage_prefix_slots = self._config_stage_prefix_slots(
            configuration_stages, stage_required_slots,
        )
        valid_start_positions = self._valid_stage_start_positions(
            company_ordered_slots, configuration_company, configurations,
            configuration_stages, stage_suffix_slots, stage_prefix_slots,
        )
        stage_position_windows = self._stage_position_windows(
            company_ordered_slots, configuration_company, configurations,
            configuration_stages, stage_required_slots,
            stage_prefix_slots, stage_suffix_slots,
        )
        covering_start_positions = self._covering_start_positions(
            valid_start_positions, stage_required_slots,
        )
        structural_reasons = self._structural_unschedulable_reasons(
            configurations, configuration_company, company_ordered_slots,
            configuration_stages, stage_resource_types,
            stage_resource_type_quantity, stage_suffix_slots,
            valid_start_positions, resource_type_active_resources,
            resource_parallel_capacity,
        )

        # Collapse interchangeable resources into pools: the model shrinks by
        # the average group size and no symmetry breaking is needed. The
        # original per-resource dicts are kept to split assignments back to
        # concrete resources after solving.
        member_parallel_capacity = resource_parallel_capacity
        member_slot_load = resource_slot_load
        (
            resource_type_active_resources, resource_parallel_capacity,
            resource_daily_capacity_minutes, resource_slot_load,
            resource_day_load,
        ) = pool_interchangeable_resources(
            resource_type_active_resources, resource_parallel_capacity,
            resource_daily_capacity_minutes, resource_slot_load,
            resource_day_load,
        )

        variable_set = self._variable_definition(
            configurations, configuration_company, company_ordered_slots,
            configuration_stages, stage_resource_types,
            stage_resource_type_quantity, resource_type_active_resources,
            valid_start_positions, stage_position_windows,
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
            resource_slot_load, resource_day_load, stage_position_windows,
            resource_slot_covered_minutes,
        )
        prob = self._solve_problem(
            objective, constraints, use_warm_start,
            time_limit=self.time_limit, gap_rel=self.gap_rel,
        )

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
            member_parallel_capacity=member_parallel_capacity,
            member_slot_load=member_slot_load,
            structural_reasons=structural_reasons,
        )
        return self._build_proposal(
            start_date=start_date, end_date=end_date,
            configuration_margin=configuration_margin,
            extracted_solution=extracted_solution,
        )
