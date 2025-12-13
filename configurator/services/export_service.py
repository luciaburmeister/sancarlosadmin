from typing import TYPE_CHECKING
from django.template import engines
from django.db.models import Q
import html
import re


if TYPE_CHECKING:
    from configurator.models import Configuration, ConfigurationExportTemplate



def strip_wrappers(raw_html: str) -> str:
    """
    Removes unwanted WYSIWYG wrappers such as:
    - <div> ... </div>
    - <p> ... </p>
    - <body> ... </body>
    - <html> ... </html>
    Returns only the internal useful markup.
    """

    if not raw_html:
        return ""

    # First unescape (&lt;div&gt; → <div>)
    cleaned = html.unescape(raw_html).strip()

    # Remove outer <div>...</div> if it wraps the whole content
    cleaned = re.sub(r'^<div[^>]*>(.*)</div>$', r'\1', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Remove <body> wrappers
    cleaned = re.sub(r'^<body[^>]*>(.*)</body>$', r'\1', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Remove <html> wrappers
    cleaned = re.sub(r'^<html[^>]*>(.*)</html>$', r'\1', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Remove <p> wrappers often added by WYSIWYG around whole content
    cleaned = re.sub(r'^<p[^>]*>(.*)</p>$', r'\1', cleaned, flags=re.DOTALL | re.IGNORECASE)

    return cleaned.strip()



def strip_css_wrappers(raw_css: str) -> str:
    if not raw_css:
        return ""

    cleaned = html.unescape(raw_css).strip()

    # Extract inside <style>...</style> if present
    style_match = re.search(r"<style[^>]*>(.*?)</style>", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if style_match:
        cleaned = style_match.group(1)

    # Remove wrapping <div> or <p>
    cleaned = re.sub(r'^<(div|p)[^>]*>(.*)</\1>$', r'\2', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Convert <br> to newlines
    cleaned = re.sub(r'<br\s*/?>', '\n', cleaned, flags=re.IGNORECASE)

    return cleaned.strip()

def clean_final_html(rendered: str) -> str:
    if not rendered:
        return rendered

    cleaned = rendered

    # 1️⃣ Remove ANY <br> that appears right after a block-level open tag
    cleaned = re.sub(
        r'(<(div|section|p|h[1-6]|table|thead|tbody|tr)[^>]*>)\s*(<br\s*/?>\s*)+',
        r'\1',
        cleaned,
        flags=re.IGNORECASE
    )

    # 2️⃣ Remove ANY <br> that appears right before a block closing tag
    cleaned = re.sub(
        r'(<br\s*/?>\s*)+(</(div|section|p|h[1-6]|table|thead|tbody|tr)>)',
        r'\2',
        cleaned,
        flags=re.IGNORECASE
    )

    # 3️⃣ Remove <br> before a <table>
    cleaned = re.sub(
        r'(<br\s*/?>\s*)+(<table)',
        r'\2',
        cleaned,
        flags=re.IGNORECASE
    )

    # 4️⃣ Remove <br> between </tr> or </td> and next block
    cleaned = re.sub(
        r'(</t[rdh]+>)\s*(<br\s*/?>\s*)+',
        r'\1',
        cleaned,
        flags=re.IGNORECASE
    )

    # 5️⃣ Remove ANY sequence of 2+ <br> tags ANYWHERE → remove completely
    cleaned = re.sub(
        r'(<br\s*/?>\s*){2,}',
        '',
        cleaned,
        flags=re.IGNORECASE
    )

    # 6️⃣ Remove stray <br> that sits alone on a line
    cleaned = re.sub(
        r'^\s*<br\s*/?>\s*$',
        '',
        cleaned,
        flags=re.MULTILINE | re.IGNORECASE
    )

    return cleaned.strip()




class ConfigurationExportService:
    """
    Service for resolving and rendering export templates for configurations.
    """

    @staticmethod
    def get_available_templates(configuration: "Configuration"):
        """
        Returns all active export templates that apply to this configuration.
        """
        from configurator.models import ConfigurationExportTemplate

        return (
            ConfigurationExportTemplate.objects.filter(
                Q(product=configuration.product) | Q(product__isnull=True),
                is_active=True,
            )
            .select_related("product")
        )

    @staticmethod
    def render_html(configuration: "Configuration", template: "ConfigurationExportTemplate"):

        # Clean HTML & CSS first
        clean_html = strip_wrappers(template.html or "")
        clean_css = strip_css_wrappers(template.css or "")

        context = {
            "config": configuration,
            "client": configuration.client,
            "items": configuration.items.all(),
            "parameters": configuration.param_values.select_related("parameter"),
            "computed": configuration.get_pricing_context(),
        }

        django_engine = engines["django"]

        user_tpl = django_engine.from_string(clean_html)
        rendered_body = user_tpl.render(context)
        rendered_body = clean_final_html(rendered_body)

        wrapper = django_engine.from_string("""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <style>
    {{ css|safe }}
    </style>
    </head>
    <body>
    {{ body|safe }}
    </body>
    </html>
    """)

        return wrapper.render({
            "body": rendered_body,
            "css": clean_css,
        })

