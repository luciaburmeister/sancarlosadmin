from typing import TYPE_CHECKING
from django.template import engines
from django.db.models import Q
import html
import re
from django.template.loader import get_template
from core.admin.company_access import get_user_companies


if TYPE_CHECKING:
    from configurator.models import Configuration, ConfigurationExportTemplate



def strip_wrappers(raw_html: str) -> str:
    """
    Removes unwanted WYSIWYG wrappers and heals fragmented blocks.
    - Removes <div> ... </div> (without attributes)
    - Removes <p> ... </p> (without attributes)
    - Heals fragmented lines (</p><p> -> ' ')
    - Removes <body> and <html> wrappers
    """

    if not raw_html:
        return ""

    # 1. First unescape (&lt;div&gt; → <div>)
    cleaned = html.unescape(raw_html).strip()

    # 2. Heal fragmentation: the editor often splits things like <svg ... > into multiple <p>
    # We join consecutive "naked" tags to restore the original flow.
    cleaned = re.sub(r'</p>\s*<p>', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'</div>\s*<div>', ' ', cleaned, flags=re.IGNORECASE)

    # 3. Remove empty/spacer tags
    cleaned = re.sub(r'<(p|div|span)[^>]*>\s*(&nbsp;)?\s*</\1>', ' ', cleaned, flags=re.IGNORECASE)

    # 4. Remove outer-most "naked" wrappers (repetitive to handle nested ones)
    for _ in range(3):
        cleaned = re.sub(r'^<div>(.*)</div>$', r'\1', cleaned, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(r'^<p>(.*)</p>$', r'\1', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # 5. Remove <body> wrappers
    cleaned = re.sub(r'^<body[^>]*>(.*)</body>$', r'\1', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # 6. Remove <html> wrappers
    cleaned = re.sub(r'^<html[^>]*>(.*)</html>$', r'\1', cleaned, flags=re.DOTALL | re.IGNORECASE)

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

    # 1️⃣ Remove <br> after any block-level opening tag
    cleaned = re.sub(
        r'(<(div|section|p|h[1-6]|table|thead|tbody|tr)[^>]*>)\s*(<br\s*/?>\s*)+',
        r'\1',
        cleaned,
        flags=re.IGNORECASE
    )

    # 2️⃣ Remove <br> before any block-level closing tag
    cleaned = re.sub(
        r'(<br\s*/?>\s*)+(</(div|section|p|h[1-6]|table|thead|tbody|tr)>)',
        r'\2',
        cleaned,
        flags=re.IGNORECASE
    )

    # 3️⃣ Remove <br> between table cells/rows
    cleaned = re.sub(
        r'(</t[rdh]+>)\s*(<br\s*/?>\s*)+',
        r'\1',
        cleaned,
        flags=re.IGNORECASE
    )

    # 4️⃣ NEW: Remove <br> between ANY closing block tag and next opening tag
    cleaned = re.sub(
        r'(</(div|section|p|h[1-6]|table|thead|tbody|tr|td|th)>)\s*<br\s*/?>',
        r'\1',
        cleaned,
        flags=re.IGNORECASE
    )

    # 5️⃣ NEW: Remove <br> before ANY block-level opening tag
    cleaned = re.sub(
        r'<br\s*/?>\s*(<(div|section|p|h[1-6]|table|thead|tbody|tr|td|th)[^>]*>)',
        r'\1',
        cleaned,
        flags=re.IGNORECASE
    )

    # 6️⃣ Remove 2+ consecutive <br> anywhere in document
    cleaned = re.sub(
        r'(<br\s*/?>\s*){2,}',
        '',
        cleaned,
        flags=re.IGNORECASE
    )

    cleaned = re.sub(
    r'(</[^>]+>)\s*<br\s*/?>\s*(<!--.*?-->\s*)?(<[^/][^>]*>)',
    r'\1\3',
    cleaned,
    flags=re.DOTALL | re.IGNORECASE
)
    cleaned = re.sub(
    r'(</span>|</strong>|</b>|</i>)\s*<br\s*/?>\s*(<strong|<span|<b|<i)',
    r'\1 \2',
    cleaned,
    flags=re.IGNORECASE
)
    cleaned = re.sub(
    r'(</div>)\s*<br\s*/?>\s*(<!--.*?-->\s*)?(<div class="section"[^>]*>)',
    r'\1 \3',
    cleaned,
    flags=re.DOTALL | re.IGNORECASE
)

    # 7️⃣ HEAL Fragmentation: Remove ALL block wrappers and breaks inside <svg> tags
    # These are introduced by WYSIWYG and break the SVG structure completely.
    def heal_svg_block(match):
        svg_content = match.group(0)
        # Remove all p, div, br tags from inside the SVG
        # SVG doesn't use these for structure (usually), so they are editor noise.
        # We replace them with spaces to avoid joining words.
        healed = re.sub(r'</?(p|div|br)[^>]*>', ' ', svg_content, flags=re.IGNORECASE)
        # Clean up double spaces
        return re.sub(r'\s+', ' ', healed)

    cleaned = re.sub(r'<svg.*?>.*?</svg>', heal_svg_block, cleaned, flags=re.DOTALL | re.IGNORECASE)

    # 8️⃣ Final clean for consecutive naked paragraphs
    cleaned = re.sub(r'</p>\s*<p>', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'</div>\s*<div>', ' ', cleaned, flags=re.IGNORECASE)

    return cleaned.strip()



class ConfigurationExportService:
    """
    Service for resolving and rendering export templates for configurations.
    """

    @staticmethod
    def get_available_templates(configuration: "Configuration", user, kind: str | None = None):
        """
        Returns all active export templates that apply to this configuration.
        """
        from configurator.models import ConfigurationExportTemplate

        user_companies = get_user_companies(user)

        filters = (Q(product=configuration.product) | Q(product__isnull=True)) \
                  & (Q(company__in=user_companies) | Q(company__isnull=True)) \
                  & Q(is_active=True)

        if kind:
            filters &= Q(kind=kind)

        return (
            ConfigurationExportTemplate.objects.filter(filters)
            .select_related("product")
        )

    @staticmethod
    def render_parts(configuration: "Configuration", template: "ConfigurationExportTemplate"):
        """
        Renders the template and returns the rendered body and clean CSS as a tuple.
        """
        # Clean HTML & CSS first
        clean_html = strip_wrappers(template.html or "")
        clean_css = strip_css_wrappers(template.css or "")

        pricing_context = configuration.get_pricing_context()

        context = {
            **pricing_context,
            "config": configuration,
            "client": configuration.client,
            "items": configuration.items.all(),
            "parameters": configuration.param_values.select_related("parameter"),
            "component_groups": configuration.component_groups.order_by("order"),
            "computed": pricing_context,
            "parameter_pricing_dictionary": pricing_context,
        }

        django_engine = engines["django"]

        user_tpl = django_engine.from_string(clean_html)
        rendered_body = user_tpl.render(context)
        rendered_body = clean_final_html(rendered_body)

        return rendered_body, clean_css

    @staticmethod
    def render_html(configuration: "Configuration", template: "ConfigurationExportTemplate"):
        rendered_body, clean_css = ConfigurationExportService.render_parts(configuration, template)

        wrapper_tpl = get_template("exports/base_export.html")

        return wrapper_tpl.render({
            "body": rendered_body,
            "css": clean_css,
        })

