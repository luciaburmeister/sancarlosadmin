from django.utils import translation
from rest_framework.parsers import JSONParser
from rest_framework.renderers import JSONRenderer
from rest_framework.views import APIView

from apps.public_api.authentication import UserAPIKeyAuthentication
from apps.public_api.exceptions import public_api_exception_handler
from apps.public_api.permissions import HasRequiredPermissions
from apps.public_api.throttling import APIKeyRateThrottle


class PublicAPIView(APIView):
    """
    Base of every public API view: API-key auth only, the user's Django
    permissions per method (``required_permissions``), a rate limit per key,
    JSON in and out, one error shape, English messages. ``self.company`` is
    the key's company: scope every query by it.
    """

    authentication_classes = [UserAPIKeyAuthentication]
    permission_classes = [HasRequiredPermissions]
    throttle_classes = [APIKeyRateThrottle]
    parser_classes = [JSONParser]
    renderer_classes = [JSONRenderer]
    required_permissions: dict[str, list[str]] = {}

    def dispatch(self, request, *args, **kwargs):
        with translation.override("en"):
            return super().dispatch(request, *args, **kwargs)

    def get_exception_handler(self):
        return public_api_exception_handler

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.company = request.auth.company
