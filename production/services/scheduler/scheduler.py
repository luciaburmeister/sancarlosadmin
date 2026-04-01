from datetime import date

from configurator.models import Configuration

class ScheduleStrategy:
    """
    The ScheduleStrategy interface declares the schedule method common to
    all schedule strategies. The context uses this interface to call the
    algorithm defined by the concrete strategies.
    """

    def schedule(
            self,
            start_date:date,
            end_date:date,
            configurations: list[Configuration],
        ):
        pass


class Scheduler:
    """
    The Scheduler takes a strategy to use to create the optimal schedule.
    """

    def __init__(self, strategy: ScheduleStrategy) -> None:
        self._strategy = strategy

    @property
    def strategy(self) -> ScheduleStrategy:
        """
        Return current ScheduleStrategy object.
        """

        return self._strategy

    @strategy.setter
    def strategy(self, strategy: ScheduleStrategy) -> None:
        """
        Replace the current ScheduleStrategy object.
        """

        self._strategy = strategy

    def schedule(
            self,
            start_date:date,
            end_date:date,
            configurations: list[Configuration]
        ):
        return self._strategy.schedule(start_date, end_date, configurations)
