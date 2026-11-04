from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_api_key.permissions import KeyParser

from apps.public_api.models import UserAPIKey, user_can_access_company

# Recording every request would be a write per call; this is precise enough
# to tell which keys are in use.
LAST_USED_RESOLUTION = timedelta(minutes=5)


class UserAPIKeyAuthentication(BaseAuthentication):
    """
    ``Authorization: Api-Key <key>``. Authenticates as the key's user and puts
    the key on ``request.auth`` (its company scopes every query).
    """

    keyword = "Api-Key"
    key_parser = KeyParser()

    def authenticate(self, request):
        raw_key = self.key_parser.get(request)
        if not raw_key:
            return None

        api_key = self._get_key(raw_key)
        if api_key.has_expired:
            raise AuthenticationFailed("API key has expired.", code="api_key_expired")
        if not api_key.user.is_active:
            raise AuthenticationFailed("Invalid API key.", code="invalid_api_key")
        if not user_can_access_company(api_key.user, api_key.company_id):
            raise AuthenticationFailed("Invalid API key.", code="invalid_api_key")

        self._touch(api_key)
        return api_key.user, api_key

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when credentials are missing or wrong
        return self.keyword

    @staticmethod
    def _get_key(raw_key: str) -> UserAPIKey:
        prefix, _, _ = raw_key.partition(".")
        try:
            api_key = (
                UserAPIKey.objects.get_usable_keys()
                .select_related("user", "company")
                .get(prefix=prefix)
            )
        except UserAPIKey.DoesNotExist:
            raise AuthenticationFailed("Invalid API key.", code="invalid_api_key")
        if not api_key.is_valid(raw_key):
            raise AuthenticationFailed("Invalid API key.", code="invalid_api_key")
        return api_key

    @staticmethod
    def _touch(api_key: UserAPIKey) -> None:
        now = timezone.now()
        if api_key.last_used_at is None or now - api_key.last_used_at > LAST_USED_RESOLUTION:
            UserAPIKey.objects.filter(pk=api_key.pk).update(last_used_at=now)
            api_key.last_used_at = now
