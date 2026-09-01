"""Fields exposed to DjangoQL search for accounting accounts and cost centers."""

from accounting.models import CompanyAccount, CostCenter
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


@register(CostCenter)
class CostCenterSearchFields(SearchFields):
    fields = (
        "id",
        "code",
        "name",
        "parent",
        "is_active",
        "is_assignable",
        "company",
    )
