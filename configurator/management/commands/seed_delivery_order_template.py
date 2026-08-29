from django.core.management.base import BaseCommand

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind

TEMPLATE_NAME = "Albarán de Entrega (por defecto)"

HTML = """
<div class="delivery-order">
  <div class="do-header">
    <div class="do-company">
      {% if company_data.logo %}<img src="{{ company_data.logo }}" class="do-logo" />{% endif %}
      <div>
        <strong>{{ company_data.legal_name|default:company_data.name }}</strong><br/>
        {{ company_data.tax_identifier|default:"" }}<br/>
        {{ company_data.address|default:"" }}<br/>
        {{ company_data.phone|default:"" }} {{ company_data.email|default:"" }}
      </div>
    </div>
    <div class="do-title-box">
      <h1>ALBARÁN DE ENTREGA</h1>
      <table>
        <tr><th>Nº Albarán:</th><td>{{ number }}</td></tr>
        <tr><th>Fecha:</th><td>{{ date|date:"d/m/Y" }}</td></tr>
        {% if project_file %}<tr><th>Expediente:</th><td>{{ project_file.reference }}</td></tr>{% endif %}
      </table>
    </div>
  </div>

  <h2>DATOS DEL CLIENTE</h2>
  <table class="do-table">
    <tr>
      <th>Nombre:</th><td>{{ client.name|default:"" }}</td>
      <th>NIF:</th><td>{{ client.tax_id|default:"" }}</td>
    </tr>
    <tr>
      <th>Teléfono:</th><td>{{ client.phone|default:"" }}</td>
      <th>E-mail:</th><td>{{ client.email|default:"" }}</td>
    </tr>
  </table>

  <h2>LÍNEAS</h2>
  <table class="do-table do-lines">
    <thead>
      <tr>
        <th>Descripción</th>
        <th class="num">Cantidad</th>
        <th class="num">Precio ud.</th>
        <th class="num">Dto. %</th>
        <th class="num">Importe</th>
      </tr>
    </thead>
    <tbody>
      {% for item in items %}
      <tr{% if item.is_discount_line %} class="do-discount-line"{% endif %}>
        <td>{{ item.description }}{% if item.reference %} ({{ item.reference }}){% endif %}</td>
        <td class="num">{% if item.is_discount_line %}—{% else %}{{ item.quantity|floatformat:"-3" }}{% endif %}</td>
        <td class="num">{% if item.is_discount_line %}—{% else %}{{ item.unit_price|floatformat:2 }} €{% endif %}</td>
        <td class="num">{% if item.discount_percentage %}{{ item.discount_percentage|floatformat:"-2" }}{% endif %}</td>
        <td class="num">{{ item.line_total|floatformat:2 }} €</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <div class="do-totals">
    <table>
      <tr><th>Subtotal:</th><td>{{ subtotal|floatformat:2 }} €</td></tr>
      {% if discount_amount %}
      <tr><th>Descuento:</th><td>-{{ discount_amount|floatformat:2 }} €</td></tr>
      {% endif %}
      {% for tax in taxes %}
      <tr>
        <th>{{ tax.name }} {{ tax.percentage|floatformat:"-2" }}%{% if tax.is_retention %} (retención){% endif %}:</th>
        <td>{% if tax.is_retention %}-{% endif %}{{ tax.amount|floatformat:2 }} €</td>
      </tr>
      {% endfor %}
      <tr class="do-total-row"><th>TOTAL:</th><td>{{ total|floatformat:2 }} €</td></tr>
    </table>
  </div>

  {% if package_count or shipping_agency or shipment_number %}
  <h2>ENVÍO</h2>
  <table class="do-table">
    <tr>
      {% if package_count %}<th>Bultos:</th><td>{{ package_count }}</td>{% endif %}
      {% if shipping_agency %}<th>Agencia:</th><td>{{ shipping_agency }}</td>{% endif %}
      {% if shipment_number %}<th>Nº envío:</th><td>{{ shipment_number }}</td>{% endif %}
    </tr>
  </table>
  {% endif %}

  {% if notes %}
  <h2>OBSERVACIONES</h2>
  <div class="do-notes">{{ notes|linebreaksbr }}</div>
  {% endif %}

  <div class="do-signatures">
    <div class="do-signature">
      <p>Recibí conforme (cliente)</p>
      <p class="do-signature-hint">Nombre, DNI, fecha y firma.</p>
    </div>
    <div class="do-signature">
      <p>Por la empresa</p>
    </div>
  </div>
</div>
"""

CSS = """
.delivery-order { font-family: Helvetica, Arial, sans-serif; font-size: 11px; color: #111; }
.do-header { display: flex; justify-content: space-between; margin-bottom: 16px; }
.do-company { display: flex; gap: 10px; align-items: flex-start; }
.do-logo { max-height: 60px; max-width: 140px; }
.do-title-box h1 { font-size: 16px; margin: 0 0 6px 0; text-align: right; }
.do-title-box table { border-collapse: collapse; margin-left: auto; }
.do-title-box th { text-align: left; padding-right: 6px; }
h2 { font-size: 12px; border-bottom: 2px solid #111; padding-bottom: 2px; margin: 14px 0 6px 0; }
.do-table { width: 100%; border-collapse: collapse; }
.do-table th { text-align: left; background: #f2f2f2; padding: 3px 6px; white-space: nowrap; }
.do-table td { padding: 3px 6px; border-bottom: 1px solid #ddd; }
.do-lines .num { text-align: right; }
.do-discount-line td { color: #b00; font-style: italic; }
.do-totals { margin-top: 10px; display: flex; justify-content: flex-end; }
.do-totals table { border-collapse: collapse; min-width: 220px; }
.do-totals th { text-align: left; padding: 3px 10px 3px 6px; }
.do-totals td { text-align: right; padding: 3px 6px; }
.do-total-row th, .do-total-row td { border-top: 2px solid #111; font-weight: bold; font-size: 12px; }
.do-notes { min-height: 40px; border: 1px solid #ccc; padding: 6px; }
.do-signatures { display: flex; gap: 20px; margin-top: 24px; }
.do-signature { flex: 1; border: 1px solid #999; min-height: 90px; padding: 6px; }
.do-signature p { margin: 0 0 4px 0; font-weight: bold; }
.do-signature-hint { font-weight: normal !important; font-size: 9px; color: #444; }
"""


class Command(BaseCommand):
    help = (
        "Seed the default 'Albarán de Entrega' export template with discount, "
        "tax and total blocks (global, no company/product)."
    )

    def handle(self, *args, **options):
        template, created = ConfigurationExportTemplate.objects.get_or_create(
            kind=ExportTemplateKind.DELIVERY_ORDER,
            name=TEMPLATE_NAME,
            product=None,
            company=None,
            defaults={"html": HTML, "css": CSS, "is_active": True},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created template '{TEMPLATE_NAME}'"))
        else:
            self.stdout.write(f"Template '{TEMPLATE_NAME}' already exists (id={template.id})")
