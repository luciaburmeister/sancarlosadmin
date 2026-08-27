"""Fields exposed to DjangoQL search for project files (expedientes)."""

from general.djangoql import SearchFields, register
from management.models import ProjectFile


@register(ProjectFile)
class ProjectFileSearchFields(SearchFields):
    fields = (
        "id",
        "reference",
        "incremental_id",
        "name",
        "status",
        "start_date",
        "end_date",
        "total_amount",
        "total_invoiced",
        "percentage_invoiced",
        "client",
        "company",
    )
