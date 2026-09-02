# company/middleware.py

from company.active_company import ActiveCompany


class ActiveCompanyMiddleware:
    """
    Resolves the current active company for the logged-in user (session,
    ~1 day, silent default) and exposes it as ``request.active_company``
    plus the request-scoped holder consumed by ``get_user_companies``.

    Must run after AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        company = None
        if request.user.is_authenticated:
            company = ActiveCompany.resolve(request)

        request.active_company = company
        ActiveCompany.set_current_id(company.pk if company else None)

        try:
            return self.get_response(request)
        finally:
            ActiveCompany.clear_current()
