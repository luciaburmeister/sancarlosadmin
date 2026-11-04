from rest_framework.permissions import BasePermission


class HasRequiredPermissions(BasePermission):
    """
    The user must hold every Django permission the view lists for the method:

        required_permissions = {"GET": ["invoices.view_salesinvoice"]}

    A method the view handles but doesn't list is denied, so a new handler
    is closed until its permissions are declared. HEAD uses GET's. A method
    the view doesn't handle passes through to DRF's 405.
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        method = "GET" if request.method == "HEAD" else request.method
        if getattr(view, method.lower(), None) is None:
            return True
        perms = getattr(view, "required_permissions", {}).get(method)
        if perms is None:
            return False
        return request.user.has_perms(perms)
