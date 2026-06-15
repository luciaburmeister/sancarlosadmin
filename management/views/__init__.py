# Re-export views so existing imports like
# `from management.views import QuoteAcceptanceWizardView` keep working.
from management.views.quote_acceptance import QuoteAcceptanceWizardView  # noqa: F401
from management.views.resource_planning_schedule import ResourcePlanningScheduleView  # noqa: F401
