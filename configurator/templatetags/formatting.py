from django import template
from django.utils.translation import gettext_lazy as _

register = template.Library()


@register.filter
def yesno_i18n(value):
    """
    Renders booleans as localized Yes / No.
    True  -> Yes / Sí
    False -> No
    """
    if value is True:
        return _("Yes")
    if value is False:
        return _("No")
    return value
