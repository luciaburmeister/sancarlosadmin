"""Fields exposed to DjangoQL search for companies."""

from company.models import Company
from general.djangoql import SearchFields, register


@register(Company)
class CompanySearchFields(SearchFields):
    fields = ("id", "name", "legal_name", "tax_identifier", "is_active")
