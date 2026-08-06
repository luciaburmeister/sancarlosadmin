from django.utils.translation import gettext as _

# Structural reasons: the configuration can never be scheduled with the
# current data, regardless of what else is competing for capacity.
NO_SCHEDULABLE_STAGES = "no_schedulable_stages"
NO_COMPANY_TIME_SLOTS = "no_company_time_slots"
HORIZON_TOO_SHORT = "horizon_too_short"
NO_ACTIVE_RESOURCES = "no_active_resources"
INSUFFICIENT_RESOURCE_CAPACITY = "insufficient_resource_capacity"

# Solver reasons: the configuration is schedulable on its own, but the
# optimizer left it out of this particular run.
NON_POSITIVE_MARGIN = "non_positive_margin"
SOLVER_INCOMPLETE = "solver_incomplete"
CAPACITY_COMPETITION = "capacity_competition"


def _resource_kind_label(kind: str) -> str:
    return _("machine") if kind == "machine" else _("employee")


def describe_reason(reason: dict) -> str:
    """
    Turns a stored reason dict ({"code": ..., **params}) into a
    human-readable, translated sentence for display.
    """
    code = reason.get("code", "")

    if code == NO_SCHEDULABLE_STAGES:
        return _(
            "The configuration has no production stage with time or resource "
            "data, so there is nothing to schedule. Check the product "
            "template's planning resources."
        )
    if code == NO_COMPANY_TIME_SLOTS:
        return _(
            "The company %(company)s has no working schedule slots on the "
            "weekdays inside the selected date range."
        ) % {"company": reason.get("company", "")}
    if code == HORIZON_TOO_SHORT:
        return _(
            "The configuration does not fit in the selected date range: its "
            "stages need %(needed)s slots (%(needed_minutes)s min) but only "
            "%(available)s slots are available."
        ) % {
            "needed": reason.get("needed_slots", "?"),
            "needed_minutes": reason.get("needed_minutes", "?"),
            "available": reason.get("available_slots", "?"),
        }
    if code == NO_ACTIVE_RESOURCES:
        return _(
            "Stage \"%(stage)s\" requires a %(kind)s, but no active %(kind)s "
            "with that stage is registered as a production resource."
        ) % {
            "stage": reason.get("stage", "?"),
            "kind": _resource_kind_label(reason.get("kind", "")),
        }
    if code == INSUFFICIENT_RESOURCE_CAPACITY:
        return _(
            "Stage \"%(stage)s\" needs %(required)s %(kind)s(s) working in "
            "parallel, but the active resources only provide a parallel "
            "capacity of %(available)s."
        ) % {
            "stage": reason.get("stage", "?"),
            "kind": _resource_kind_label(reason.get("kind", "")),
            "required": reason.get("required", "?"),
            "available": reason.get("available", "?"),
        }
    if code == NON_POSITIVE_MARGIN:
        return _(
            "The configuration's margin is not positive (%(margin)s), so the "
            "margin-maximizing strategy will never select it."
        ) % {"margin": reason.get("margin", "?")}
    if code == SOLVER_INCOMPLETE:
        return _(
            "The solver stopped before finding a complete solution (status: "
            "%(status)s). The configuration might still fit; try a longer "
            "time limit or fewer configurations."
        ) % {"status": reason.get("status", "?")}
    if code == CAPACITY_COMPETITION:
        return _(
            "The configuration is schedulable, but there was not enough "
            "remaining resource capacity or working time in the date range "
            "once the selected configurations were scheduled."
        )

    return code
