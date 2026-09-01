"""
Neutral starting point handed to the AI generator when a template has no html/css yet
(instead of an empty string). Kind-agnostic: logo/header/footer shell and brand
colors/fonts only, no document-specific content - matches the existing house style
(see migration 0054_default_sales_invoice_template) so every new kind inherits the
same visual identity rather than each being generated independently from a blank page.
"""

DEFAULT_SKELETON_HTML = """
<div class="doc-container">
    <header>
        <div class="company-logo">
            <h1>{{ company.name|default:"POLIPUS ERP" }}</h1>
        </div>
        <div class="doc-title">
            <h2>DOCUMENT TITLE</h2>
        </div>
    </header>

    <div class="doc-body">
        <!-- Document-specific content goes here -->
    </div>

    <footer>
        <p class="legal-notice">Gracias por su confianza.</p>
    </footer>
</div>
""".strip()

DEFAULT_SKELETON_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');

body {
    font-family: 'Inter', sans-serif;
    color: #1f2937;
    line-height: 1.5;
    margin: 0;
    padding: 0;
}

.doc-container {
    max-width: 800px;
    margin: 40px auto;
    padding: 40px;
    background: #fff;
}

header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    border-bottom: 2px solid #64A7A5;
    padding-bottom: 20px;
    margin-bottom: 30px;
}

.company-logo h1 {
    color: #64A7A5;
    margin: 0;
    font-size: 24px;
}

.doc-title h2 {
    margin: 0;
    font-size: 32px;
    color: #111827;
}

footer {
    margin-top: 60px;
    padding-top: 20px;
    border-top: 1px solid #e5e7eb;
    text-align: center;
    color: #6b7280;
    font-size: 12px;
}
""".strip()
