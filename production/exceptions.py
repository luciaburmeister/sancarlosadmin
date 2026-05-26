"""Domain exceptions for production workflows."""


class StageHasActiveWorkersError(Exception):
    """Raised when finishing a stage while other workers still have open sessions."""

    code = "stage_has_active_workers"

    def __init__(self, active_workers: list[dict], message: str | None = None):
        self.active_workers = active_workers
        self.message = message or (
            "No puedes finalizar: hay otros trabajadores activos en esta etapa."
        )
        super().__init__(self.message)
