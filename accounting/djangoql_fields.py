"""Fields exposed to DjangoQL search for accounting accounts."""

from accounting.models import CompanyAccount
from general.djangoql import SearchFields, register


@register(CompanyAccount)
class CompanyAccountSearchFields(SearchFields):
    fields = (
        "id",
        "code",
        "name",
        "account_type",
        "is_active",
        "is_postable",
        "company",
    )
