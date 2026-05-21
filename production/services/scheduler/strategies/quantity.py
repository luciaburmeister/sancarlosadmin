import pulp

from production.services.scheduler.strategies.margin import MarginScheduler

class QuantityScheduler(MarginScheduler):
    def _objective_definition(
        self, variable_set, configuration_margin, configurations,
        configuration_stages, valid_start_positions,
    ):
        count = pulp.lpSum(
            variable_set["y"][c] for c in configuration_margin
        )

        # Penalize ending later than necessary
        EPS = 1e-3
        late_penalty = pulp.lpSum(
            pos * variable_set["start"][(c, s, pos)]
            for c in configurations
            for s in configuration_stages[c]
            for pos in valid_start_positions[(c, s)]
        )

        return count - EPS * late_penalty
