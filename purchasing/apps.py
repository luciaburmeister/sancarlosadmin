from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class PurchasingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'purchasing'
    verbose_name = _('Purchasing')

    def ready(self):
        import purchasing.signals  # noqa: F401
