from django.db import migrations

# Default agent reports, after the monthly reports Fistex Energy sends each
# agent. Shared by every company until it adds agent_report templates of its own.

CSS = """
@page {
    margin: 12mm 12mm 16mm;
    @bottom-right { content: none; }
    @bottom-center {
        content: counter(page) "/" counter(pages);
        font-family: 'Inter', sans-serif;
        font-size: 8pt;
        font-weight: 700;
        color: #222;
    }
}
body { font-size: 7.5pt; color: #222; }

.report-header { display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 1px solid #444; padding-bottom: 4px; margin-bottom: 14px; }
.report-logo img { max-height: 46px; max-width: 170px; }
.report-heading { text-align: right; }
.report-date { font-size: 8pt; margin-bottom: 14px; }
.report-heading h1 { font-size: 15pt; font-weight: 700; margin: 0; }

table.report-bar { border: 1px solid #444; background: #c8c8c8; margin-bottom: 8px; }
table.report-bar td { padding: 1px 6px; font-weight: 700; }
table.report-bar td.code { width: 1%; white-space: nowrap; border-right: 1px solid #444; background: #fff; }
table.report-bar td.period { text-align: right; font-weight: 400; font-style: italic; }

h2 { font-size: 11pt; font-weight: 700; margin: 6px 0 2px; border-bottom: 1px solid #444; }

/* Each column header is underlined on its own. */
table.report-table { table-layout: fixed; border-collapse: separate; border-spacing: 5px 0; }
.report-table th { font-weight: 700; text-align: left; border-bottom: 1px solid #444; padding: 1px 0; white-space: nowrap; overflow: hidden; }
.report-table td { padding: 1px 0; vertical-align: top; white-space: nowrap; overflow: hidden; }
.report-table .num { text-align: right; }
.report-table tfoot td { padding-top: 3px; }
.report-table tfoot td.sum { border-top: 1px solid #444; }
.report-table tfoot.bold td { font-weight: 700; }
.report-table .spacer td { height: 10px; }
.report-table .empty { color: #777; padding: 8px 0; }
.report-table .sub { font-size: 6.5pt; color: #555; }

/* Invoice lines, italic, under their own headers. */
.report-table td.lines-cell { padding: 2px 0 8px; white-space: normal; }
table.report-lines { table-layout: fixed; border-collapse: separate; border-spacing: 5px 0; font-size: 6.5pt; }
.report-lines th { font-weight: 700; text-align: left; border-bottom: 1px solid #444; padding: 0; white-space: nowrap; }
.report-lines td { font-style: italic; padding: 1px 0; white-space: nowrap; overflow: hidden; }
.report-lines .num { text-align: right; }

.vat-summary { page-break-before: always; }
.vat-summary h2 { font-size: 13pt; margin-bottom: 14px; }
table.vat-table { width: 70%; margin: 0 auto; border: 1px solid #444; border-collapse: collapse; }
.vat-table th, .vat-table td { padding: 1px 6px; text-align: right; border-right: 1px solid #444; }
.vat-table th { font-weight: 700; border-bottom: 1px solid #444; }
.vat-table tfoot td { font-weight: 700; border-top: 1px solid #444; }
.vat-table td.name { text-align: left; }
""".strip()

HEADER = """
<div class="report-header">
    <div class="report-logo">
        {% if company_data.logo %}<img src="{{ company_data.logo }}" alt="{{ company.name }}">{% else %}<strong>{{ company.name }}</strong>{% endif %}
    </div>
    <div class="report-heading">
        <div class="report-date">{{ printed_on|date:"d/m/Y" }}</div>
        <h1>__TITLE__</h1>
    </div>
</div>
<table class="report-bar">
    <tr>
        <td class="code">Agente: {{ agent.pk }}</td>
        <td>{{ agent.name }}</td>
        <td class="period">{{ date_from|date:"d/m/Y" }} – {{ date_to|date:"d/m/Y" }}</td>
    </tr>
</table>
"""

DOCUMENT_COLUMNS = """
    <colgroup>
        <col style="width: 11%"><col style="width: 9%"><col style="width: 5%"><col style="width: 19%"><col style="width: 5%"><col style="width: 11%"><col style="width: 8%"><col style="width: 5%"><col style="width: 8%"><col>
    </colgroup>
    <thead>
        <tr>
            <th>Número</th><th>Fecha</th><th class="num">Cliente</th><th>Nombre</th><th class="num">Agente</th>
            <th>F.Pago</th><th class="num">Base</th><th class="num">Irpf</th><th class="num">Iva+Rec.</th><th class="num">Total</th>
        </tr>
    </thead>
"""

DOCUMENT_ROW = """
            <tr>
                <td>{{ row.number }}</td>
                <td>{{ row.date|date:"d/m/Y" }}</td>
                <td class="num">{{ row.client_code|default:"" }}</td>
                <td>{{ row.client_name }}</td>
                <td class="num">{{ agent.pk }}</td>
                <td>{{ row.payment_method }}</td>
                <td class="num">{{ row.base|floatformat:"2g" }}</td>
                <td class="num">{{ row.retention|floatformat:"2g" }}</td>
                <td class="num">{{ row.taxes|floatformat:"2g" }}</td>
                <td class="num">{{ row.total|floatformat:"2g" }}</td>
            </tr>
"""

DOCUMENT_TOTALS = """
        <tr class="spacer"><td colspan="10"></td></tr>
    </tbody>
    <tfoot>
        <tr>
            <td class="sum num">{{ __DATA__.rows|length }}</td>
            <td colspan="5"></td>
            <td class="sum num">{{ __DATA__.totals.base|floatformat:"2g" }}</td>
            <td class="sum num">{{ __DATA__.totals.retention|floatformat:"2g" }}</td>
            <td class="sum num">{{ __DATA__.totals.taxes|floatformat:"2g" }}</td>
            <td class="sum num">{{ __DATA__.totals.total|floatformat:"2g" }}</td>
        </tr>
    </tfoot>
</table>
"""

INVOICE_LINES = """
            <tr>
                <td colspan="10" class="lines-cell">
                    <table class="report-lines">
                        <colgroup>
                            <col style="width: 11%"><col style="width: 40%"><col style="width: 8%"><col style="width: 10%"><col style="width: 8%"><col style="width: 10%"><col>
                        </colgroup>
                        <thead>
                            <tr>
                                <th>Código</th><th>Denominación</th><th class="num">Cantidad</th><th class="num">Precio</th>
                                <th class="num">% Desc</th><th class="num">Importe</th><th></th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for item in row.items %}
                                <tr>
                                    <td>{{ item.reference }}</td>
                                    <td>{{ item.description }}</td>
                                    <td class="num">{{ item.quantity|floatformat:"-2" }}</td>
                                    <td class="num">{{ item.unit_price|floatformat:"-2g" }}</td>
                                    <td class="num">{% if item.discount_percentage %}{{ item.discount_percentage|floatformat:"-2" }}%{% endif %}</td>
                                    <td class="num">{{ item.line_total|floatformat:"2g" }}</td>
                                    <td></td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </td>
            </tr>
"""

VAT_SUMMARY = """
{% if invoices.vat_summary %}
<div class="vat-summary">
    <h2>Cuadro resumen de IVA</h2>
    <table class="vat-table">
        <thead>
            <tr><th class="name">Impuesto</th><th>Tipo</th><th>Base imponible</th><th>Cuota</th><th>Registros</th></tr>
        </thead>
        <tbody>
            {% for group in invoices.vat_summary %}
                <tr>
                    <td class="name">{{ group.name }}</td>
                    <td>{{ group.percentage|floatformat:"1" }}%</td>
                    <td>{{ group.base|floatformat:"2g" }}</td>
                    <td>{{ group.amount|floatformat:"2g" }}</td>
                    <td>{{ group.count }}</td>
                </tr>
            {% endfor %}
        </tbody>
        <tfoot>
            <tr>
                <td class="name" colspan="2">Total</td>
                <td>{{ invoices.vat_totals.base|floatformat:"2g" }}</td>
                <td>{{ invoices.vat_totals.amount|floatformat:"2g" }}</td>
                <td></td>
            </tr>
        </tfoot>
    </table>
</div>
{% endif %}
"""


def invoices_html(title, lines):
    return (
        '<div class="report">'
        + HEADER.replace("__TITLE__", title)
        + '<table class="report-table">'
        + DOCUMENT_COLUMNS
        + "<tbody>{% for row in invoices.rows %}"
        + DOCUMENT_ROW
        + lines
        + '{% empty %}<tr><td colspan="10" class="empty">No hay facturas en este periodo.</td></tr>{% endfor %}'
        + DOCUMENT_TOTALS.replace("__DATA__", "invoices")
        + VAT_SUMMARY
        + "</div>"
    )


QUOTES_HTML = (
    '<div class="report">'
    + HEADER.replace("__TITLE__", "Informe de presupuestos de clientes")
    + '<table class="report-table">'
    + DOCUMENT_COLUMNS
    + "<tbody>{% for row in quotes.rows %}"
    + DOCUMENT_ROW
    + '{% empty %}<tr><td colspan="10" class="empty">No hay presupuestos en este periodo.</td></tr>{% endfor %}'
    + DOCUMENT_TOTALS.replace("__DATA__", "quotes")
    + "</div>"
)

SETTLEMENT_HTML = (
    '<div class="report">'
    + HEADER.replace("__TITLE__", "Informe de comisiones y gastos de agentes")
    + """
<h2>Comisiones</h2>
<table class="report-table">
    <colgroup>
        <col style="width: 9%"><col style="width: 18%"><col style="width: 6%"><col style="width: 34%"><col style="width: 9%"><col style="width: 6%"><col>
    </colgroup>
    <thead>
        <tr>
            <th>Fecha</th><th class="num">Documento</th><th class="num">Cliente</th><th>Nombre del cliente</th>
            <th class="num">Base</th><th class="num">%Com.</th><th class="num">Comisión</th>
        </tr>
    </thead>
    <tbody>
        {% for row in settlement.rows %}
            <tr>
                <td>{{ row.date|date:"d/m/Y" }}</td>
                <td class="num">
                    {{ row.document }}
                    {% if row.delivery_orders %}<div class="sub">Albaranes: {{ row.delivery_orders }}</div>{% endif %}
                </td>
                <td class="num">{{ row.client_code }}</td>
                <td>{{ row.client_name }}</td>
                <td class="num">{{ row.base|floatformat:"2g" }}</td>
                <td class="num">{{ row.percentage|floatformat:"-2" }}</td>
                <td class="num">{{ row.commission|floatformat:"2g" }}</td>
            </tr>
        {% empty %}
            <tr><td colspan="7" class="empty">No se cobró ninguna comisión en este periodo.</td></tr>
        {% endfor %}
    </tbody>
    <tfoot class="bold">
        <tr>
            <td colspan="4"></td>
            <td class="sum num">{{ settlement.totals.base|floatformat:"2g" }}</td>
            <td class="sum num">{{ settlement.totals.percentage|floatformat:"-2" }}</td>
            <td class="sum num">{{ settlement.totals.commission|floatformat:"2g" }}</td>
        </tr>
    </tfoot>
</table>
</div>"""
)

TEMPLATES = {
    "Liquidación de agente": SETTLEMENT_HTML,
    "Facturación de agente": invoices_html("Informe de facturas de clientes", ""),
    "Facturación detallada de agente": invoices_html("Informe de facturas de clientes", INVOICE_LINES),
    "Presupuestos de agente": QUOTES_HTML,
}


def create_default_agent_report_templates(apps, schema_editor):
    ConfigurationExportTemplate = apps.get_model("configurator", "ConfigurationExportTemplate")
    for name, html in TEMPLATES.items():
        ConfigurationExportTemplate.objects.get_or_create(
            name=name,
            kind="agent_report",
            company=None,
            product=None,
            defaults={"html": html, "css": CSS, "subtype": "document", "is_active": True},
        )


def remove_default_agent_report_templates(apps, schema_editor):
    ConfigurationExportTemplate = apps.get_model("configurator", "ConfigurationExportTemplate")
    ConfigurationExportTemplate.objects.filter(
        name__in=TEMPLATES, kind="agent_report", company=None,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("configurator", "0107_agent_report_export_kinds"),
    ]

    operations = [
        migrations.RunPython(create_default_agent_report_templates, reverse_code=remove_default_agent_report_templates),
    ]
