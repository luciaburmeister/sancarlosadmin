# company/unfold.py
#
# Callbacks referenced by dotted string from the UNFOLD settings dict
# (SITE_DROPDOWN / ENVIRONMENT). Unfold calls them with the current request.

from django.urls import reverse
from django.utils.translation import gettext as _

from company.active_company import ActiveCompany


def company_switch_dropdown(request):
    """
    Items for the sidebar-header dropdown: one entry per switchable
    company (see the template override at
    templates/unfold/helpers/site_dropdown.html). Checking/unchecking items
    is purely client-side (Alpine state) — nothing is submitted until the
    dropdown's "Apply" button POSTs the whole pending selection at once to
    admin:company_switch_active_apply, which replaces the active set in a
    single request. `link` is unused by our template but required by
    Unfold's DropdownItem; it points at the legacy single-company toggle
    endpoint for parity if the vendor template is ever restored.
    Hidden for single-company non-superusers.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return []

    companies = list(ActiveCompany.switchable_for(user).order_by("name"))
    if not user.is_superuser and len(companies) < 2:
        return []

    active_ids = {c.pk for c in getattr(request, "active_companies", [])}
    switch_url = reverse("admin:company_switch_active")

    return [
        {
            "title": company.name,
            "link": switch_url,
            "icon": "check_box" if company.pk in active_ids else "check_box_outline_blank",
            "attrs": {
                "company_id": company.pk,
                "active": company.pk in active_ids,
            },
        }
        for company in companies
    ]


def active_company_badge(request):
    """Shows the active companies as a badge in the admin top header."""
    companies = getattr(request, "active_companies", None) or []
    if not companies:
        return None
    if len(companies) <= 2:
        return [" · ".join(c.name for c in companies), "primary"]
    return [_("%(count)d empresas") % {"count": len(companies)}, "primary"]
