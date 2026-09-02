# company/unfold.py
#
# Callbacks referenced by dotted string from the UNFOLD settings dict
# (SITE_DROPDOWN / ENVIRONMENT). Unfold calls them with the current request.

from django.urls import reverse

from company.active_company import ActiveCompany


def company_switch_dropdown(request):
    """
    Items for the sidebar-header dropdown: one entry per switchable
    company, POSTing to admin:company_switch_active (see the template
    override at templates/unfold/helpers/site_dropdown.html, which reads
    company_id/next from item.attrs). Hidden for single-company
    non-superusers.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return []

    companies = list(ActiveCompany.switchable_for(user).order_by("name"))
    if not user.is_superuser and len(companies) < 2:
        return []

    active_id = getattr(getattr(request, "active_company", None), "pk", None)
    switch_url = reverse("admin:company_switch_active")
    next_url = request.get_full_path()

    return [
        {
            "title": company.name,
            "link": switch_url,
            "icon": "check" if company.pk == active_id else "domain",
            "attrs": {"company_id": company.pk, "next": next_url},
        }
        for company in companies
    ]


def active_company_badge(request):
    """Shows the active company as a badge in the admin top header."""
    company = getattr(request, "active_company", None)
    if company is None:
        return None
    return [company.name, "primary"]
