# Import all admin modules so Django's autodiscover picks them up.
from management.admin.project_file import *  # noqa: F401, F403
from management.admin.resource_planning import *  # noqa: F401, F403
from management.admin.work_entry_type import *  # noqa: F401, F403
