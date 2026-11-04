"""OpenAPI (drf-spectacular) hooks: only the public API is documented."""
from drf_spectacular.extensions import OpenApiAuthenticationExtension

PUBLIC_API_PREFIX = "/api/v1/"


def only_public_api(endpoints, **kwargs):
    """Preprocessing hook: drop every endpoint outside the public API (the mobile API, docs views)."""
    return [
        endpoint for endpoint in endpoints
        if endpoint[0].startswith(PUBLIC_API_PREFIX)
        and not endpoint[0].startswith((f"{PUBLIC_API_PREFIX}schema/", f"{PUBLIC_API_PREFIX}docs/"))
    ]


class UserAPIKeyScheme(OpenApiAuthenticationExtension):
    target_class = "apps.public_api.authentication.UserAPIKeyAuthentication"
    name = "ApiKeyAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "`Api-Key <your key>`. Keys are created in the ERP admin and act as their user.",
        }
