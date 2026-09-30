TICKET_STAGE_LOOKUPS_KEY = "production:ticket_filters:stages"
TICKET_STAGE_STATUS_LOOKUPS_KEY = "production:ticket_filters:stage_statuses"
LOOKUPS_CACHE_TIMEOUT = 60  #seconds 

class ProductionActions:
    START_ACTIVITY = "start_activity"
    FINISH_ACTIVITY = "finish_activity"
    START_SESSION = "start_session"
    END_SESSION = "end_session"
    ADVANCE_STAGE = "advance_stage"
    UNDO_FINISH = "undo_finish"
    REOPEN_STAGE = "reopen_stage"
    
    # Kanban Actions
    UPDATE_STATUS = "update_status"
    ASSIGN_EMPLOYEE = "assign_employee"
    UNASSIGN_EMPLOYEE = "unassign_employee"

    # Assembly Line Actions
    START_LINE = "start_line"
    PAUSE_LINE = "pause_line"
    FINISH_LINE = "finish_line"
    JOIN_LINE = "join_line"
    START_STAGE = "start_stage"


class ProductionPermissions:
    """Django permission codenames (see production.models.Meta.permissions)."""

    REORDER_ITEMS = "production.can_reorder_items"
    RESCHEDULE_SESSIONS = "production.can_reschedule_planned_sessions"


class PlanningActions:
    """Mutations the planning calendar posts to its action endpoint."""

    MOVE_SESSION = "move_session"
    RESIZE_SESSION = "resize_session"
    SET_RESOURCES = "set_resources"
    UNPLAN_SESSION = "unplan_session"
    PLAN_TICKET = "plan_ticket"
    PLAN_TICKET_CUSTOM = "plan_ticket_custom"
    PLAN_TICKET_STAGE_CUSTOM = "plan_ticket_stage_custom"
    REORDER_TICKET = "reorder_ticket"


# A ticket's colour is its id modulo the palette -- stable, and never stored.
TICKET_PALETTE = [
    "#f5a524",  # amber
    "#4cc2e0",  # sky
    "#f0629b",  # pink
    "#9b8cf5",  # violet
    "#8bd450",  # lime
    "#f97362",  # coral
    "#48c9b0",  # teal
    "#d4a5f5",  # lilac
    "#e5c454",  # gold
    "#6fb1f0",  # blue
]
