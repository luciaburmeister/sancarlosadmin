from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PublicApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.public_api"
    verbose_name = _("Public API")

    def ready(self):
        from apps.public_api import schema  # noqa: F401 — registers the OpenAPI auth extension
