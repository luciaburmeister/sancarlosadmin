from production.services.scheduler.strategies.margin import MarginScheduler
from production.services.scheduler.strategies.quantity import QuantityScheduler
from production.services.scheduler.core import ScheduleStrategy


def build_scheduler_strategy(
    strategy_code: str,
    optimization_params: dict,
) -> ScheduleStrategy:
    if strategy_code == "margin":
        return MarginScheduler(optimization_params=optimization_params)
    elif strategy_code == "quantity":
        return QuantityScheduler(optimization_params=optimization_params)

    raise ValueError(f"Unknown scheduling strategy: {strategy_code}")