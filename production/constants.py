from django.db import models

TICKET_STAGE_LOOKUPS_KEY = "production:ticket_filters:stages"
TICKET_STAGE_STATUS_LOOKUPS_KEY = "production:ticket_filters:stage_statuses"
LOOKUPS_CACHE_TIMEOUT = 60  #seconds 

class ProductionActions(models.TextChoices):
    START_ACTIVITY = "start_activity"
    FINISH_ACTIVITY = "finish_activity"
    START_SESSION = "start_session"
    FINISH_SESSION = "finish_session"
    ADVANCE_STAGE = "advance_stage"