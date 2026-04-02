from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any

@dataclass
class ScheduledAssignment:
    configuration_id: int
    configuration_name: str
    configuration_item_id: int
    item_name: str
    stage_name: str
    resource_id: int
    resource_name: str
    copy_index: int
    start: datetime
    end: datetime
    slot_count: int
    minutes: int


@dataclass
class ScheduleProposal:
    start_date: date
    end_date: date
    slot_minutes: int
    solver_status: str
    is_optimal: bool
    objective_value: float | None
    total_margin: float
    selected_configuration_ids: list[int] = field(default_factory=list)
    unselected_configuration_ids: list[int] = field(default_factory=list)
    configuration_margins: dict[int, float] = field(default_factory=dict)
    assignments: list[ScheduledAssignment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
