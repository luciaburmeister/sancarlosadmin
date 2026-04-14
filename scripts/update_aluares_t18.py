import os
import sys
import django

# Setup Django environment
sys.path.append('/Users/joangaes/Documents/Scalewave/fistex/alumniniooerp')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'alumniniooerp.settings')
django.setup()

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind

def create_consolidated_template():
    try:
        # We use ID 2 as the exact gold standard for Aluares look & feel
        base_template = ConfigurationExportTemplate.objects.get(id=2)
    except ConfigurationExportTemplate.DoesNotExist:
        print("Base Aluares template (ID 2) not found.")
        return

    # THE GOAL: Mirror the Aluares "look" (ID 2) exactly for the consolidated quote.
    
    # --- HTML DEFINITION ---
    # We use the exact snippets from Aluares (ID 2) for header, meta, and summary boxes.
    new_html = """
<div class="quote-wrapper">

<!-- PAGE 1: RESUMEN GENERAL (Summary Page) -->
<div class="page-summary">
    
    <!-- EXACT ALUARES HEADER -->
    <div class="header">
        <div class="logo-box">
            <img src="{{ quote.company.logo.url }}" class="company-logo">
        </div>
        <div class="company-box">
            <h1>PRESUPUESTO</h1>
            <p><strong>{{ quote.company.legal_name }}</strong></p>
            <p>{{ quote.company.address }}</p>
            <p>Tel: {{ quote.company.phone }}</p>
            <p>Email: {{ quote.company.email }}</p>
        </div>
        <div class="client-box">
            <h1>DATOS CLIENTE</h1>
            <p><strong>{{ client.name }}</strong></p>
            <p>Tel: {{ client.phone }}</p>
            <p>Email: {{ client.email }}</p>
            <p>{{ client.address }}</p>
        </div>
    </div>

    <!-- EXACT ALUARES META BOX -->
    <div class="meta-box">
        <div><strong>Documento:</strong> {{ quote.pk }}</div>
        <div><strong>Fecha:</strong> {{ quote.created_at|date:"d/m/Y" }}</div>
    </div>

    <!-- SUMMARY TABLE (Consolidated items list) -->
    <div class="section">
        <h2>Descripción de la Oferta</h2>
        <table class="data-table">
            <thead>
                <tr>
                    <th>Ref.</th>
                    <th>Producto / Configuración</th>
                    <th class="right">Base</th>
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

    <br>

    <!-- EXACT ALUARES SUMMARY BOX STYLE -->
    <div class="summary-box">
        <div class="summary-row">
            <span class="label">BASE IMPONIBLE</span>
            <span class="value">{{ subtotal_price|floatformat:2 }} €</span>
        </div>

        {% if tax_breakdown %}
        <div class="summary-break"></div>
        <div class="tax-title">Impuestos</div>
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

        <div class="summary-total mt-3">
            <span class="label mt-2">TOTAL (impuestos incluidos)</span>
            <span class="value">{{ total_price|floatformat:2 }} €</span>
        </div>
    </div>

    <br>

    <!-- EXACT ALUARES FOOTER / BANK -->
    <div class="footer-box">
        <p><strong>Condiciones de pago:</strong> 50% pedido · 50% entrega.</p>
    </div>

    <section class="bank-section">
        <div class="bank-grid">
            <div class="bank-row"> <strong>Forma de pago:</strong> Transferencia </div>
            <div class="bank-row"><span class="bank-label">Cuenta de pago:</span></div>
            <div class="bank-row"><span class="bank-label">Titular:</span><span> ALUCASA LACALUM S.L </span></div>
            <div class="bank-row"><span class="bank-label">Banco:</span><span> Banco Sabadell </span></div>
            <div class="bank-row"><span class="bank-label">IBAN:</span><span>ES54 0081 0587 3700 0149 5755</span></div>
            <div class="bank-row"><span class="bank-label">BIC:</span><span> BSABESBBXXX </span></div>
            <div style="margin-top: 10px;">No están incluidos trabajos de albañilería, acometidas eléctricas, medios de elevación así como lo no indicado en la descripción del producto.</div>
        </div>
    </section>

</div> <!-- End Page 1 -->

<!-- DETAIL PAGES (One per configuration, mirroring the Aluares per-config layout) -->
{% for config_ctx in configurations %}
<div class="page-detail" style="page-break-before: always; border-top: 1px solid #eee; padding-top: 20px;">
    
    <div class="header">
        <div class="logo-box">
            <img src="{{ quote.company.logo.url }}" class="company-logo" style="width: 120px;">
        </div>
        <div style="flex: 1; text-align: right;">
            <h2 style="margin: 0; font-size: 16px;">Anexo de Detalle #{{ forloop.counter }}</h2>
            <p style="font-size: 12px; margin: 0;">Presupuesto {{ quote.pk }}</p>
        </div>
    </div>

    <!-- EXACT ALUARES PRODUCT BOX -->
    <div class="product-box">
        <div><strong>Configuración:</strong> {{ config_ctx.config.name }}</div>
        <div><strong>Producto:</strong> {{ config_ctx.config.product.name }}</div>
        <div class="full">
            <strong>Descripción:</strong> {{ config_ctx.config.product.description }}
        </div>
    </div>

    <!-- EXACT ALUARES PARAMETERS SECTION -->
    <div class="section">
        <h2>Especificaciones de la configuración</h2>
        <table class="data-table">
            <thead>
                <tr>
                    <th>Característica</th>
                    <th>Valor</th>
                </tr>
            </thead>
            <tbody>
                {% for parameter in config_ctx.parameters %}
                {% if parameter.parameter.label != "HORAS MANO DE OBRA" and parameter.parameter.label != "COSTE INSTALACIÓN" %}
                <tr>
                    <td>{{ parameter.parameter.label }}</td>
                    <td>{{ parameter.value|yesno_i18n }}</td>
                </tr>
                {% endif %}
                {% endfor %}
            </tbody>
        </table>
    </div>

    <!-- DISCOUNTS PER ITEM -->
    {% if config_ctx.discounts %}
    <div class="section">
        <h2>Ajustes y Descuentos</h2>
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

    <!-- PER ITEM TOTALS -->
    <div class="summary-box" style="margin-top: 20px;">
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

    # --- CSS DEFINITION ---
    # We use EXACTLY the CSS from Aluares (ID 2) + our specialized pagination rules.
    new_css = base_template.css + """
/* Consolidated Quote Overrides */
@media print {
    .page-summary { page-break-after: always; }
}
.page-detail {
    margin-top: 30px;
}
.product-box {
    margin-top: 15px;
}
"""

    consolidated_template, created = ConfigurationExportTemplate.objects.get_or_create(
        id=18,
        defaults={
            'kind': ExportTemplateKind.CONSOLIDATED_QUOTATION,
            'name': "PRESUPUESTO CONSOLIDADO ALUARES",
            'company': base_template.company,
            'is_active': True,
        }
    )

    consolidated_template.html = new_html
    consolidated_template.css = new_css
    consolidated_template.save()

    print(f"Template '{consolidated_template.name}' (ID: {consolidated_template.id}) has been updated to match Aluares format exactly.")

if __name__ == "__main__":
    create_consolidated_template()
