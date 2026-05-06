import pulp

from production.services.scheduler.schedule_margin import MarginScheduler

class QuantityScheduler(MarginScheduler):
    def _objective_definition(
        self,
        variable_set,
        configuration_margin,
        configurations,
        configuration_items,
        item_stages,
        valid_start_positions,
    ):
        margin = pulp.lpSum(
            variable_set["y"][c] for c in configuration_margin
        )

        # Penalize ending later than necessary
        EPS = 1e-3

        late_penalty = pulp.lpSum(
            pos * variable_set["start"][(c, i, s, pos)]
            for c in configurations
            for i in configuration_items[c]
            for s in item_stages[i]
            for pos in valid_start_positions[(c, i, s)]
        )

        return margin - EPS * late_penalty
