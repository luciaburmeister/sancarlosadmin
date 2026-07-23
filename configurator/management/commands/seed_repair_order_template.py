from django.core.management.base import BaseCommand

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind

TEMPLATE_NAME = "Orden de Reparación (por defecto)"

HTML = """
<div class="repair-order">
  <div class="ro-header">
    <div class="ro-company">
      {% if company_data.logo %}<img src="{{ company_data.logo }}" class="ro-logo" />{% endif %}
      <div>
        <strong>{{ company_data.legal_name|default:company_data.name }}</strong><br/>
        {{ company_data.tax_identifier|default:"" }}<br/>
        {{ company_data.address|default:"" }}<br/>
        {{ company_data.phone|default:"" }} {{ company_data.email|default:"" }}
      </div>
    </div>
    <div class="ro-title-box">
      <h1>ORDEN DE REPARACIÓN</h1>
      <table>
        <tr><th>Nº O.R.:</th><td>{{ reference }}</td></tr>
        <tr><th>Fecha de recepción:</th><td>{{ date|date:"d/m/Y" }}</td></tr>
        {% if end_date %}<tr><th>Fecha estimada de entrega:</th><td>{{ end_date|date:"d/m/Y" }}</td></tr>{% endif %}
      </table>
    </div>
  </div>

  <h2>DATOS DEL CLIENTE</h2>
  <table class="ro-table">
    <tr>
      <th>Nombre:</th><td>{{ client_name|default:"" }}</td>
      <th>NIF:</th><td>{{ client_tax_id|default:"" }}</td>
    </tr>
    <tr>
      <th>Teléfono:</th><td>{{ client_phone|default:"" }}</td>
      <th>E-mail:</th><td>{{ client_email|default:"" }}</td>
    </tr>
    <tr>
      <th>Dirección:</th><td colspan="3">{{ client_address|default:"" }}</td>
    </tr>
  </table>

  {% if asset %}
  <h2>DATOS DEL VEHÍCULO</h2>
  <table class="ro-table">
    <tr>
      {% for ident in identifiers %}
      <th>{{ ident.label }}:</th><td>{{ ident.value }}</td>
      {% if forloop.counter|divisibleby:3 and not forloop.last %}</tr><tr>{% endif %}
      {% endfor %}
    </tr>
    <tr>
      {% for field in asset_fields %}
      <th>{{ field.label }}:</th><td>{{ field.value|default:"" }}</td>
      {% if forloop.counter|divisibleby:3 and not forloop.last %}</tr><tr>{% endif %}
      {% endfor %}
    </tr>
  </table>
  {% endif %}

  {% if reception_fields %}
  <h2>ESTADO DEL VEHÍCULO</h2>
  <table class="ro-table">
    <tr>
      {% for field in reception_fields %}
      <th>{{ field.label }}:</th><td>{{ field.value|default:"" }}</td>
      {% if forloop.counter|divisibleby:3 and not forloop.last %}</tr><tr>{% endif %}
      {% endfor %}
    </tr>
  </table>
  {% endif %}

  <h2>OBSERVACIONES / ACTUACIONES A REALIZAR</h2>
  <div class="ro-observations">
    {{ description|linebreaksbr }}
    {% if reception_notes %}<br/>{{ reception_notes|linebreaksbr }}{% endif %}
  </div>

  <div class="ro-signatures">
    <div class="ro-signature">
      <p>Firma del cliente</p>
      <p class="ro-signature-hint">Autorizo las reparaciones descritas, así como las pruebas que
      puedan ser necesarias para las mismas.</p>
    </div>
    <div class="ro-signature">
      <p>Firma del taller</p>
    </div>
  </div>

  <div class="ro-legal">
    <p>La emisión de la presente orden de reparación implica que el cliente acepta las condiciones
    de uso y la política de privacidad del taller. Los datos serán utilizados con la única finalidad
    de prestar el servicio contratado y no serán cedidos a terceros salvo obligación legal. El
    interesado podrá ejercer sus derechos de acceso, rectificación, supresión y demás derechos
    reconocidos dirigiéndose a la dirección indicada en esta orden.</p>
    <p>El cliente tiene derecho a la elaboración de un presupuesto previo. Todas las reparaciones
    están garantizadas según la legislación vigente. El taller no se hace responsable de los objetos
    depositados en el interior del vehículo.</p>
  </div>
</div>
"""

CSS = """
.repair-order { font-family: Helvetica, Arial, sans-serif; font-size: 11px; color: #111; }
.ro-header { display: flex; justify-content: space-between; margin-bottom: 16px; }
.ro-company { display: flex; gap: 10px; align-items: flex-start; }
.ro-logo { max-height: 60px; max-width: 140px; }
.ro-title-box h1 { font-size: 16px; margin: 0 0 6px 0; text-align: right; }
.ro-title-box table { border-collapse: collapse; margin-left: auto; }
.ro-title-box th { text-align: left; padding-right: 6px; }
h2 { font-size: 12px; border-bottom: 2px solid #111; padding-bottom: 2px; margin: 14px 0 6px 0; }
.ro-table { width: 100%; border-collapse: collapse; }
.ro-table th { text-align: left; background: #f2f2f2; padding: 3px 6px; white-space: nowrap; }
.ro-table td { padding: 3px 6px; border-bottom: 1px solid #ddd; }
.ro-observations { min-height: 70px; border: 1px solid #ccc; padding: 6px; }
.ro-signatures { display: flex; gap: 20px; margin-top: 24px; }
.ro-signature { flex: 1; border: 1px solid #999; min-height: 90px; padding: 6px; }
.ro-signature p { margin: 0 0 4px 0; font-weight: bold; }
.ro-signature-hint { font-weight: normal !important; font-size: 9px; color: #444; }
.ro-legal { margin-top: 18px; font-size: 8px; color: #444; }
"""


class Command(BaseCommand):
    help = "Seed the default 'Orden de Reparación' export template (global, no company/product)."

    def handle(self, *args, **options):
        template, created = ConfigurationExportTemplate.objects.get_or_create(
            kind=ExportTemplateKind.REPAIR_ORDER,
            name=TEMPLATE_NAME,
            product=None,
            company=None,
            defaults={"html": HTML, "css": CSS, "is_active": True},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created template '{TEMPLATE_NAME}'"))
        else:
            self.stdout.write(f"Template '{TEMPLATE_NAME}' already exists (id={template.id})")
