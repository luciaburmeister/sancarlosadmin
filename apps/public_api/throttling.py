from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class APIKeyRateThrottle(SimpleRateThrottle):
    """Requests per API key, at ``settings.PUBLIC_API_RATE`` (e.g. "600/min")."""

    scope = "public_api"

    def get_rate(self):
        return settings.PUBLIC_API_RATE

    def get_cache_key(self, request, view):
        if request.auth is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.auth.prefix}
