from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PayrollConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payroll"
    verbose_name = _("Payroll")