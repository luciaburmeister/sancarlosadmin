"""
Neutral starting point handed to the AI generator when a template has no html/css yet
(instead of an empty string). Kind-agnostic: logo/header/footer shell and brand
colors/fonts only, no document-specific content - matches the existing house style
(see migration 0054_default_sales_invoice_template) so every new kind inherits the
same visual identity rather than each being generated independently from a blank page.

Written for paper, not for a browser viewport: the output is always an A4 PDF from
WeasyPrint. Page size, margins, page numbers, the Inter @font-face and table
page-break defaults come from exports/_print_defaults.css, injected by the wrapper,
so the template itself never needs @page, @import or a centred max-width container.
"""

DEFAULT_SKELETON_HTML = """
<div class="doc-container">
    <header class="doc-header">
        <div class="company-block">
            {% if company.logo %}
            <img class="company-logo" src="{{ company.logo.url }}" alt="{{ company.name }}">
            {% else %}
            <h1>{{ company.name|default:"POLIPUS ERP" }}</h1>
            {% endif %}
        </div>
        <div class="doc-title">
            <h2>DOCUMENT TITLE</h2>
        </div>
    </header>

    <div class="doc-body">
        <!-- Document-specific content goes here. Use <table> for anything with
             columns (line items, totals, address blocks side by side). -->
    </div>

    <footer class="doc-footer">
        <p class="legal-notice">Gracias por su confianza.</p>
    </footer>
</div>
""".strip()

DEFAULT_SKELETON_CSS = """
/* Fonts, @page (A4, margins, page numbers) and table page-break rules are provided
   by the shared print base. Sizes below are in mm/pt: this renders on paper. */

body {
    font-family: 'Inter', sans-serif;
    font-size: 10pt;
    line-height: 1.4;
    color: #1f2937;
}

.doc-container {
    width: 100%;
}

.doc-header {
    width: 100%;
    border-bottom: 0.6mm solid #64A7A5;
    padding-bottom: 5mm;
    margin-bottom: 8mm;
}

.doc-header::after {
    content: "";
    display: block;
    clear: both;
}

.company-block {
    float: left;
    width: 55%;
}

.company-logo {
    max-height: 18mm;
    width: auto;
}

.company-block h1 {
    color: #64A7A5;
    margin: 0;
    font-size: 18pt;
    font-weight: 700;
}

.doc-title {
    float: right;
    width: 45%;
    text-align: right;
}

.doc-title h2 {
    margin: 0;
    font-size: 22pt;
    font-weight: 700;
    color: #111827;
}

.doc-body table {
    width: 100%;
    border-collapse: collapse;
}

.doc-body th {
    background: #f9fafb;
    text-align: left;
    font-size: 8pt;
    font-weight: 600;
    text-transform: uppercase;
    padding: 2.5mm 2mm;
    border-bottom: 0.5mm solid #e5e7eb;
}

.doc-body td {
    padding: 2.5mm 2mm;
    border-bottom: 0.2mm solid #f3f4f6;
    vertical-align: top;
}

.text-right {
    text-align: right;
}

.doc-footer {
    margin-top: 12mm;
    padding-top: 4mm;
    border-top: 0.2mm solid #e5e7eb;
    text-align: center;
    color: #6b7280;
    font-size: 8pt;
}
""".strip()
