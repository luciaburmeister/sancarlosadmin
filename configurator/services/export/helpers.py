import html
import re

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
    cleaned = re.sub(r'<(p|div|span)\b[^>]*>\s*(&nbsp;)?\s*</\1>', ' ', cleaned, flags=re.IGNORECASE)

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
    cleaned = re.sub(r'^<(div|p)\b[^>]*>(.*)</\1>$', r'\2', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Convert <br> to newlines
    cleaned = re.sub(r'<br\s*/?>', '\n', cleaned, flags=re.IGNORECASE)

    return cleaned.strip()

def clean_final_html(rendered: str) -> str:
    if not rendered:
        return rendered

    cleaned = rendered

    # 1️⃣ Remove <br> after any block-level opening tag
    cleaned = re.sub(
        r'(<(div|section|p|h[1-6]|table|thead|tbody|tr)\b[^>]*>)\s*(<br\s*/?>\s*)+',
        r'\1',
        cleaned,
        flags=re.IGNORECASE
    )

    # 2️⃣ Remove <br> before any block-level closing tag
    cleaned = re.sub(
        r'(<br\s*/?>\s*)+(</(div|section|p|h[1-6]|table|thead|tbody|tr)\b>)',
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
        r'(</(div|section|p|h[1-6]|table|thead|tbody|tr|td|th)\b>)\s*<br\s*/?>',
        r'\1',
        cleaned,
        flags=re.IGNORECASE
    )

    # 5️⃣ NEW: Remove <br> before ANY block-level opening tag
    cleaned = re.sub(
        r'<br\s*/?>\s*(<(div|section|p|h[1-6]|table|thead|tbody|tr|td|th)\b[^>]*>)',
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
    def heal_svg_block(match):
        svg_content = match.group(0)
        healed = re.sub(r'</?(p|div|br)\b[^>]*>', ' ', svg_content, flags=re.IGNORECASE)
        return re.sub(r'\s+', ' ', healed)

    cleaned = re.sub(r'<svg.*?>.*?</svg>', heal_svg_block, cleaned, flags=re.DOTALL | re.IGNORECASE)

    # 8️⃣ Final clean for consecutive naked paragraphs
    cleaned = re.sub(r'</p>\s*<p\s*>', ' ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'</div>\s*<div\s*>', ' ', cleaned, flags=re.IGNORECASE)

    return cleaned.strip()
