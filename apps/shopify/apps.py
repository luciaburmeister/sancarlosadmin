from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ShopifyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.shopify"
    verbose_name = _("Shopify")
