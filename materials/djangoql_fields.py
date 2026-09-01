"""Fields exposed to DjangoQL search for providers."""

from general.djangoql import SearchFields, has_value, is_set, register
from materials.models import Provider


@register(Provider)
class ProviderSearchFields(SearchFields):
    fields = (
        "id",
        "name",
        "contact",
        "email",
        "phone",
        "tax_identifier",
        "account",
        "default_cost_center",
        "company",
        has_value("has_tax_identifier", "tax_identifier", "A NIF / CIF is on file."),
        has_value("has_email", "email", "An email address is on file."),
        is_set("has_account", "account", "An accounting account is assigned."),
        is_set("has_cost_center", "default_cost_center", "A default cost center is assigned."),
    )
