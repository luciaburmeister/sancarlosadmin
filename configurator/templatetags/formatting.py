import decimal
from decimal import Decimal, InvalidOperation
from django import template
from django.utils.translation import gettext_lazy as _

register = template.Library()

@register.filter
def yesno_i18n(value):
    """
    Renders booleans as localized Yes / No.
    Handles both actual booleans and their string representations.
    """
    if value is True or str(value).lower() in ["true", "1", "yes"]:
        return _("Yes")
    if value is False or str(value).lower() in ["false", "0", "no"]:
        return _("No")
    return value


@register.filter
def euro_format(value):
    """
    Formats a number in European style: 1.234,56
    Always shows two decimal places.
    """
    if value is None or value == "":
        return "0,00"
        
    try:
        # If it's already a number type
        if isinstance(value, (int, float, Decimal)):
            val = Decimal(str(value))
        else:
            # It's a string or other type.
            s = str(value).strip()
            if "," in s and "." in s:
                # 1.234,56 style -> replace dot with nothing, comma with dot
                s = s.replace(".", "").replace(",", ".")
            elif "," in s:
                # 1234,56 style -> replace comma with dot
                s = s.replace(",", ".")
            # else it's 1234.56 or 1234 style
            
            val = Decimal(s)
            
        # Standard formatting: 1,234.56
        formatted = "{:,.2f}".format(val)
        # Swap separators: 1,234.56 -> 1.234,56
        return formatted.replace(",", "TEMP").replace(".", ",").replace("TEMP", ".")
    except (ValueError, InvalidOperation, TypeError):
        # If we can't parse it, return the original value
        return value
