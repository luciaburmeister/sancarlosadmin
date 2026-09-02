# company/middleware.py

from company.active_company import ActiveCompany


class ActiveCompanyMiddleware:
    """
    Resolves the active companies for the logged-in user (session, ~1 day,
    silent default to one company) and exposes them as
    ``request.active_companies`` (list sorted by name) plus the
    request-scoped holder consumed by ``get_user_companies``.

    Must run after AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        companies = []
        if request.user.is_authenticated:
            companies = ActiveCompany.resolve(request)

        request.active_companies = companies
        ActiveCompany.set_current_ids([c.pk for c in companies] or None)

        try:
            return self.get_response(request)
        finally:
            ActiveCompany.clear_current()
