from django.core.management.base import BaseCommand

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind

TEMPLATE_NAME = "Solicitud de Cotización (por defecto)"

HTML = """
<div class="quote-request">
  <div class="qr-header">
    <div class="qr-company">
      {% if company_data.logo %}<img src="{{ company_data.logo }}" class="qr-logo" />{% endif %}
      <div>
        <strong>{{ company_data.legal_name|default:company_data.name }}</strong><br/>
        {{ company_data.tax_identifier|default:"" }}<br/>
        {{ company_data.address|default:"" }}<br/>
        {{ company_data.phone|default:"" }} {{ company_data.email|default:"" }}
      </div>
    </div>
    <div class="qr-title-box">
      <h1>SOLICITUD DE COTIZACIÓN</h1>
      <table>
        <tr><th>Nº:</th><td>{{ number }}</td></tr>
        <tr><th>Fecha:</th><td>{{ date_requested|date:"d/m/Y" }}</td></tr>
        <tr><th>Solicitado por:</th><td>{{ requested_by_name }}</td></tr>
      </table>
    </div>
  </div>

  <h2>ARTÍCULOS SOLICITADOS</h2>
  <table class="qr-table">
    <thead>
      <tr>
        <th>Descripción</th>
        <th>Cantidad</th>
        <th>Unidad</th>
      </tr>
    </thead>
    <tbody>
      {% for item in items %}
      <tr>
        <td>{{ item.description }}</td>
        <td>{{ item.quantity }}</td>
        <td>{{ item.unit }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% if notes %}
  <h2>OBSERVACIONES</h2>
  <div class="qr-observations">
    {{ notes|linebreaksbr }}
  </div>
  {% endif %}
</div>
"""

CSS = """
.quote-request { font-family: Helvetica, Arial, sans-serif; font-size: 11px; color: #111; }
.qr-header { display: flex; justify-content: space-between; margin-bottom: 16px; }
.qr-company { display: flex; gap: 10px; align-items: flex-start; }
.qr-logo { max-height: 60px; max-width: 140px; }
.qr-title-box h1 { font-size: 16px; margin: 0 0 6px 0; text-align: right; }
.qr-title-box table { border-collapse: collapse; margin-left: auto; }
.qr-title-box th { text-align: left; padding-right: 6px; }
h2 { font-size: 12px; border-bottom: 2px solid #111; padding-bottom: 2px; margin: 14px 0 6px 0; }
.qr-table { width: 100%; border-collapse: collapse; }
.qr-table th { text-align: left; background: #f2f2f2; padding: 3px 6px; }
.qr-table td { padding: 3px 6px; border-bottom: 1px solid #ddd; }
.qr-observations { min-height: 40px; border: 1px solid #ccc; padding: 6px; }
"""


class Command(BaseCommand):
    help = "Seed the default 'Solicitud de Cotización' export template (global, no company/product)."

    def handle(self, *args, **options):
        template, created = ConfigurationExportTemplate.objects.get_or_create(
            kind=ExportTemplateKind.QUOTE_REQUEST,
            name=TEMPLATE_NAME,
            product=None,
            company=None,
            defaults={"html": HTML, "css": CSS, "is_active": True},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created template '{TEMPLATE_NAME}'"))
        else:
            self.stdout.write(f"Template '{TEMPLATE_NAME}' already exists (id={template.id})")
