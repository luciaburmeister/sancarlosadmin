from production.services.scheduler.schedule_margin import MarginScheduler
from production.services.scheduler.scheduler import ScheduleStrategy


def build_scheduler_strategy(
    strategy_code: str,
    optimization_params: dict,
) -> ScheduleStrategy:
    if strategy_code == "margin":
        return MarginScheduler(optimization_params=optimization_params)

    raise ValueError(f"Unknown scheduling strategy: {strategy_code}")