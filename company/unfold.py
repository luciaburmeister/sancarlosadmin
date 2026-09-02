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
    company, POSTing to admin:company_switch_active (see the template
    override at templates/unfold/helpers/site_dropdown.html, which reads
    company_id/next from item.attrs). Each click toggles that company in
    or out of the active set, so the icon is a checkbox. Hidden for
    single-company non-superusers.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return []

    companies = list(ActiveCompany.switchable_for(user).order_by("name"))
    if not user.is_superuser and len(companies) < 2:
        return []

    active_ids = {c.pk for c in getattr(request, "active_companies", [])}
    switch_url = reverse("admin:company_switch_active")
    next_url = request.get_full_path()

    return [
        {
            "title": company.name,
            "link": switch_url,
            "icon": "check_box" if company.pk in active_ids else "check_box_outline_blank",
            "attrs": {
                "company_id": company.pk,
                "next": next_url,
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
