"""Fields exposed to DjangoQL search for clients, zones and agents."""

from django.db.models import F, Q

from general.djangoql import SearchFields, flag, has_value, is_set, register
from sales.models import Client, CommissionistAgent, SaleZone

# Compared through a subquery on Client rather than a plain
# `Q(current_risk__gt=F("max_risk"))`: django-money only builds the companion
# currency comparison correctly when both money fields sit on the queryset's
# own model, so the direct form breaks as soon as the flag is reached through a
# relation (`client.exceeds_risk_limit` in a sales invoice search).
EXCEEDS_RISK_LIMIT = flag(
    "exceeds_risk_limit",
    lambda: Q(
        pk__in=Client.objects.filter(current_risk__gt=F("max_risk")).values("pk")
    ),
    "Current risk is above the client's maximum allowed risk.",
)


@register(Client)
class ClientSearchFields(SearchFields):
    fields = (
        "id",
        "name",
        "commercial_name",
        "tax_identifier",
        "industry_identifier",
        "nima",
        "email",
        "phone",
        "address",
        "is_insured",
        "max_risk",
        "current_risk",
        "payment_method",
        "payment_terms_days",
        "sale_zone",
        "agent",
        "company",
        has_value("has_tax_identifier", "tax_identifier", "A NIF / CIF is on file."),
        has_value("has_email", "email", "An email address is on file."),
        is_set(
            "terms_accepted",
            "terms_accepted_at",
            "The client has signed the terms & conditions.",
        ),
        EXCEEDS_RISK_LIMIT,
    )


@register(SaleZone)
class SaleZoneSearchFields(SearchFields):
    fields = ("id", "name", "company")


@register(CommissionistAgent)
class CommissionistAgentSearchFields(SearchFields):
    fields = (
        "id",
        "name",
        "tax_identifier",
        "email",
        "phone",
        "commission_percentage",
        "is_active",
        "company",
    )
