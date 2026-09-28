import base64
import decimal
import io
from decimal import Decimal, InvalidOperation

import qrcode
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


def format_hours_minutes(value):
    """
    Formats decimal hours as 'Xh Ym' (e.g. 7.84 -> '7h 50m'). Handles
    negative values (e.g. a labor-margin deficit) with a leading '-'.
    Plain function (not just a registered filter) so it's also importable
    directly for use outside templates, e.g. in admin @display methods
    that build HTML via f-strings.
    """
    if value is None or value == "":
        return "0h 0m"
    try:
        hours = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError):
        return value

    negative = hours < 0
    hours = abs(hours)
    total_minutes = int((hours * 60).to_integral_value(rounding=decimal.ROUND_HALF_UP))
    h, m = divmod(total_minutes, 60)
    return f"{'-' if negative else ''}{h}h {m}m"


register.filter("hours_minutes", format_hours_minutes)


def qr(value):
    """PNG data URI of a QR encoding the given link. Empty when there is nothing to encode."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""

    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    code.add_data(text)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


register.filter("qr", qr)
