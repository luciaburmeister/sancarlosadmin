from django.db import migrations


# (code, name, type) — codes observed in the A3 export sample.
# Accruals appear above "TOTAL BRUTO"; deductions below it.
SEED_CONCEPTS = [
    ("1", "Salario Base", "accrual"),
    ("3", "Mejora Voluntaria", "accrual"),
    ("4", "Antigüedad", "accrual"),
    ("8", "Plus Actividad", "accrual"),
    ("30", "P.P. Extras", "accrual"),
    ("34", "Plus Disponibilidad", "accrual"),
    ("63", "Plus Higiene", "accrual"),
    ("87", "Plus Pan", "accrual"),
    ("315", "Planes de pensiones", "accrual"),
    ("398", "Plus Transporte", "accrual"),
    ("402", "Cuota autonomo", "accrual"),
    ("450", "Prest.Enfermedad Cargo Empresa", "accrual"),
    ("451", "Enfermedad", "accrual"),
    ("452", "Accidente", "accrual"),
    ("455", "Complemento I.T.", "accrual"),
    ("703", "Embargo Salarial", "deduction"),
    ("789", "Dcto.Conceptos en Especie", "deduction"),
    ("995", "Cotización Contingencias Comunes", "deduction"),
    ("996", "Cotización Formación Profesional", "deduction"),
    ("997", "Cotización Desempleo", "deduction"),
    ("999", "Tributación I.R.P.F.", "deduction"),
]


def seed_concepts(apps, schema_editor):
    PayrollConcept = apps.get_model("payroll", "PayrollConcept")
    for code, name, concept_type in SEED_CONCEPTS:
        PayrollConcept.objects.get_or_create(
            code=code,
            defaults={"name": name, "type": concept_type},
        )


def unseed_concepts(apps, schema_editor):
    PayrollConcept = apps.get_model("payroll", "PayrollConcept")
    codes = [c for c, _n, _t in SEED_CONCEPTS]
    PayrollConcept.objects.filter(code__in=codes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0002_employeepayrollprofile_a3_employee_code"),
    ]

    operations = [
        migrations.RunPython(seed_concepts, unseed_concepts),
    ]
