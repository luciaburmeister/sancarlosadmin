TICKET_STAGE_LOOKUPS_KEY = "production:ticket_filters:stages"
TICKET_STAGE_STATUS_LOOKUPS_KEY = "production:ticket_filters:stage_statuses"
LOOKUPS_CACHE_TIMEOUT = 60  #seconds 

class ProductionActions:
    START_ACTIVITY = "start_activity"
    FINISH_ACTIVITY = "finish_activity"
    START_SESSION = "start_session"
    END_SESSION = "end_session"
    ADVANCE_STAGE = "advance_stage"
    
    # Kanban Actions
    UPDATE_STATUS = "update_status"
    ASSIGN_EMPLOYEE = "assign_employee"
    UNASSIGN_EMPLOYEE = "unassign_employee"