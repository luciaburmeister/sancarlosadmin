"""Guard against drivers acting on a plan scheduled for a later day.

/plannings/next/ shows tomorrow's first plan read-only once today's queue is
empty (``next_day_task``). An old app build or a stale screen could still post
an action for it, so every driver entry point that starts or advances a plan
calls :func:`future_planning_error` and returns its response when not None."""
from django.http import JsonResponse
from django.utils import timezone

FUTURE_PLANNING_CODE = "future_planning"


def future_planning_error(planning):
    """400 JsonResponse when ``planning`` is scheduled after today, else None."""
    if planning is None or planning.scheduled_date is None:
        return None
    if planning.scheduled_date <= timezone.localdate():
        return None
    fecha = planning.scheduled_date.strftime("%d/%m/%Y")
    return JsonResponse({
        "error": f"Esta tarea es de {fecha}. No puedes empezarla hoy.",
        "code": FUTURE_PLANNING_CODE,
    }, status=400)
