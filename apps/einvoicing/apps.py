from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class EInvoicingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.einvoicing"
    verbose_name = _("E-Invoicing")
