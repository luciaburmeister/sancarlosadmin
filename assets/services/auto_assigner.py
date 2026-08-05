"""
Small assignment helpers shared across the app.

The batch/bulk algorithm lives in assets/services/transport_assigner.py
(TransportAssigner) — this module now only holds the pieces that are used
outside of that: the urgency ordering constant, and auto_link_transport
(the synchronous truck-from-driver auto-link used by a single drag on the
schedule view, assets/views/asset_schedule.py).
"""
from __future__ import annotations

from django.utils.translation import gettext as _

from assets.models import Asset, AssetPlanning, AssetTypeCategory, PlanningUrgency
from company.models import Employee

URGENCY_ORDER = {
    PlanningUrgency.URGENT: 0,
    PlanningUrgency.HIGH: 1,
    PlanningUrgency.MEDIUM: 2,
    PlanningUrgency.LOW: 3,
}


def auto_link_transport(
    planning: AssetPlanning, employee: Employee, previous_driver_id: int | None = None
) -> str | None:
    """
    A truck is no longer required to drag a planning onto the schedule — instead,
    when it doesn't have one yet, try to pick it up from the assigned driver: the
    truck(s) directly associated to that employee (Asset.assigned_to).

    Never overrides a truck someone actually chose. The one exception: if the
    driver just changed and the current transport_asset turns out to be the
    *previous* driver's own truck (i.e. it only got there via this same
    auto-link, not a deliberate pick), it's cleared and re-resolved against
    the new driver below — otherwise reassigning the job would silently leave
    the old driver's truck on it forever.

    suggested_trucks is never cleared here, only ever added to — it's kept as
    an accumulating hint for whoever assigns this planning later (by hand or
    by TransportAssigner), so a driver reassignment can only grow it.

    Returns a warning note for the frontend to surface when the card still
    needs manual attention (no truck found, or several candidates left for a
    human to pick).

    Separate from TransportAssigner: this runs synchronously off a single
    driver drag on the schedule view, not a date-wide batch pass.
    """
    if planning.transport_asset_id:
        driver_changed = previous_driver_id is not None and previous_driver_id != employee.pk
        carried_over_from_previous_driver = driver_changed and Asset.objects.filter(
            pk=planning.transport_asset_id, assigned_to_id=previous_driver_id
        ).exists()
        if not carried_over_from_previous_driver:
            return None
        planning.transport_asset = None

    trucks = list(
        Asset.objects.filter(
            assigned_to=employee,
            asset_type__category=AssetTypeCategory.TRUCK,
            is_active=True,
            company_id=planning.company_id,
        ).order_by("name")
    )
    if len(trucks) == 1:
        planning.transport_asset = trucks[0]
        planning.save(update_fields=["transport_asset"])
        return None
    if len(trucks) > 1:
        planning.suggested_trucks.add(*trucks)
        return str(
            _("%(employee)s has more than one truck — added as suggestions, pick one manually.")
            % {"employee": employee}
        )
    return str(_("%(employee)s has no truck assigned — pick one manually.") % {"employee": employee})
