"""Domain exceptions for production workflows."""

from django.utils.translation import gettext_lazy as _


class StageHasActiveWorkersError(Exception):
    """Raised when finishing a stage while other workers still have open sessions."""

    code = "stage_has_active_workers"

    def __init__(self, active_workers: list[dict], message: str | None = None):
        self.active_workers = active_workers
        self.message = message or _(
            "Can't finish: other workers are still active on this stage."
        )
        super().__init__(self.message)


class PlannedSessionConflictError(Exception):
    """Raised when a manual reschedule/create would overlap another planned
    session on the same resource."""

    code = "planned_session_conflict"

    def __init__(self, conflicting_session_id: int, message: str | None = None):
        self.conflicting_session_id = conflicting_session_id
        self.message = message or _(
            "That time overlaps another already-planned session."
        )
        super().__init__(self.message)
