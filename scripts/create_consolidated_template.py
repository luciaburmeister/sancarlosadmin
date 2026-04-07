import os
import sys
import django

# Setup Django environment
sys.path.append('/Users/joangaes/Documents/Scalewave/fistex/alumniniooerp')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'alumniniooerp.settings')
django.setup()

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from django.utils.translation import gettext_lazy as _

def create_consolidated_template():
    try:
        base_template = ConfigurationExportTemplate.objects.get(id=2)
    except ConfigurationExportTemplate.DoesNotExist:
        print("Base Aluares template (ID 2) not found.")
        return

    # Adapt HTML for Multi-Page Consolidated Quotation with explicit variable usage
    new_html = """
<div class="quote-wrapper">

<!-- PAGE 1: RESUMEN DEL PRESUPUESTO -->
<div class="page-summary">
    <!-- HEADER -->
    <div class="header">
     <div class="logo-box">
     <img src="{{ quote.company.logo.url }}" class="company-logo">
     </div>
     <div class="company-box">
     <h1>PRESUPUESTO</h1>
     <p><strong>{{quote.company.legal_name}}</strong></p>
     <p>{{quote.company.address}}</p>
     <p>Tel: {{quote.company.phone}}</p>
     <p>Email: {{quote.company.email}}</p>
     </div>
     <div class="client-box">
     <h1>DATOS CLIENTE</h1>
     <p><strong>{{ client.name }}</strong></p>
     <p>Tel: {{ client.phone }}</p>
     <p>Email: {{ client.email }}</p>
     <p>{{ client.address }}</p>
     </div>
    </div>

    <!-- META -->
    <div class="meta-box">
    <div><strong>Presupuesto Nº:</strong> {{ quote.pk }}</div>
    <div><strong>Fecha:</strong> {{ quote.created_at|date:"d/m/Y" }}</div>
    </div>

    <!-- PRODUCT SUMMARY TABLE -->
    <div class="section">
     <h2>Resumen de Productos Incluidos</h2>
     <table class="data-table">
     <thead>
     <tr>
     <th>Nº</th>
     <th>Descripción del Producto</th>
     <th class="right">Subtotal</th>
     <th class="right">Total</th>
     </tr>
     </thead>
     <tbody>
     {% for config_ctx in configurations %}
     <tr>
     <td>{{ forloop.counter }}</td>
     <td>
     <strong>{{ config_ctx.config.product.name }}</strong><br>
     <small>{{ config_ctx.config.name }}</small>
     </td>
     <td class="right">{{ config_ctx.subtotal|floatformat:2 }} €</td>
     <td class="right">{{ config_ctx.total|floatformat:2 }} €</td>
     </tr>
     {% endfor %}
     </tbody>
     </table>
    </div>

    <!-- SUMMARY FINANCIALS -->
    <div class="summary-box">
        <div class="summary-row">
            <span class="label">BASE IMPONIBLE</span>
            <span class="value">{{ subtotal_price|floatformat:2 }} €</span>
        </div>

        {% if tax_breakdown %}
        <div class="summary-break"></div>
        <div class="tax-title">Desglose de Impuestos</div>
        <table class="tax-table">
        <thead>
        <tr>
        <th>Concepto</th>
        <th>Base</th>
        <th>%</th>
        <th class="right">Importe</th>
        </tr>
        </thead>
        <tbody>
        {% for t in tax_breakdown %}
        <tr>
        <td><strong>{{ t.tax_name }}</strong></td>
        <td class="right">{{ t.base|floatformat:2 }} €</td>
        <td class="right">{{ t.percentage }}%</td>
        <td class="right">{{ t.tax_amount|floatformat:2 }} €</td>
        </tr>
        {% endfor %}
        </tbody>
        </table>
        {% endif %}

        <div class="summary-break"></div>

        <div class="summary-total">
            <span class="label">TOTAL PRESUPUESTO</span>
            <span class="value">{{ total_price|floatformat:2 }} €</span>
        </div>
    </div>

    <!-- FOOTER INFO -->
    <div class="footer-box" style="margin-top: 20px;">
        <p><strong>Condiciones de pago:</strong> 50% pedido · 50% entrega.</p>
        <section class="bank-section">
            <div class="bank-grid">
                <div class="bank-row"><strong>IBAN:</strong> ES54 0081 0587 3700 0149 5755 (Banco Sabadell)</div>
            </div>
        </section>
    </div>
</div>

<!-- PAGES 2+: DETALLES DE CADA PRODUCTO -->
{% for config_ctx in configurations %}
<div class="page-detail" style="page-break-before: always;">
    <div class="header">
        <div class="logo-box">
            <img src="{{ quote.company.logo.url }}" class="company-logo" style="width: 150px;">
        </div>
        <div class="title-box">
            <h2>Detalle de Producto #{{ forloop.counter }}</h2>
            <p><strong>{{ config_ctx.config.product.name }}</strong> ({{ config_ctx.config.name }})</p>
        </div>
    </div>

    <!-- PARAMETERS / CONFIGURATION DETAILS -->
    <div class="section">
        <h3>Especificaciones de la Configuración</h3>
        <table class="data-table">
        <thead>
        <tr>
        <th>Característica</th>
        <th>Valor Seleccionado</th>
        </tr>
        </thead>
        <tbody>
        {% for parameter in config_ctx.parameters %}
        <tr>
        <td>{{ parameter.parameter.label }}</td>
        <td>{{ parameter.value|yesno_i18n }}</td>
        </tr>
        {% endfor %}
        </tbody>
        </table>
    </div>

    <!-- DISCOUNTS IF ANY -->
    {% if config_ctx.discounts %}
    <div class="section">
        <h3>Ajustes y Descuentos del Producto</h3>
        <table class="tax-table">
        <thead>
        <tr>
        <th>Concepto</th>
        <th class="right">Importe</th>
        </tr>
        </thead>
        <tbody>
        {% for line in config_ctx.discounts %}
        <tr>
        <td><strong>{{ line.label }}</strong></td>
        <td class="right">{{ line.amount|floatformat:2 }} €</td>
        </tr>
        {% endfor %}
        </tbody>
        </table>
    </div>
    {% endif %}

    <!-- LINE TOTAL FOR THIS CONFIG -->
    <div class="summary-box">
        <div class="summary-row">
            <span class="label">SUBTOTAL PRODUCTO</span>
            <span class="value">{{ config_ctx.subtotal|floatformat:2 }} €</span>
        </div>
        <div class="summary-total">
            <span class="label">TOTAL PRODUCTO</span>
            <span class="value">{{ config_ctx.total|floatformat:2 }} €</span>
        </div>
    </div>
</div>
{% endfor %}

</div>
"""

    # Add page-break CSS
    new_css = base_template.css + """
@media print {
    .page-summary { page-break-after: always; }
    .page-detail { page-break-before: always; }
}
.page-detail { margin-top: 40px; }
.title-box h2 { margin: 0; font-size: 18px; color: #333; }
.bank-row { font-size: 11px; }
"""

    consolidated_template, created = ConfigurationExportTemplate.objects.get_or_create(
        kind=ExportTemplateKind.CONSOLIDATED_QUOTATION,
        company=base_template.company,
        name="PRESUPUESTO CONSOLIDADO ALUARES",
        defaults={
            'html': new_html,
            'css': new_css,
            'is_active': True,
        }
    )

    if not created:
        consolidated_template.html = new_html
        consolidated_template.css = new_css
        consolidated_template.save()

    print(f"Template '{consolidated_template.name}' (ID: {consolidated_template.id}) has been updated with explicit data paths.")

if __name__ == "__main__":
    create_consolidated_template()
