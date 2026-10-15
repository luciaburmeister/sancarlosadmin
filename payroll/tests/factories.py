"""Shared payroll test fixtures: the payroll company and its employees."""
from datetime import date

from company.models import Company, Employee


def make_company(*, name="Test Company", tax_identifier="B12345678"):
    """Create the company whose NIF the importer workbooks carry."""
    return Company.objects.create(name=name, tax_identifier=tax_identifier)


def make_employee(*, company, first_name, last_name, hired_at=date(2020, 1, 1), **kwargs):
    """Create an Employee hired on 2020-01-01 unless told otherwise."""
    return Employee.objects.create(
        company=company, first_name=first_name, last_name=last_name,
        hired_at=hired_at, **kwargs,
    )


def make_importer_employees(*, company, dnis=None):
    """Create JUAN GARCIA LOPEZ and ANA PEREZ SANZ, the two workbook employees."""
    extra = [{"identification_number": dni} for dni in dnis] if dnis else [{}, {}]
    return (
        make_employee(company=company, first_name="JUAN", last_name="GARCIA LOPEZ", **extra[0]),
        make_employee(company=company, first_name="ANA", last_name="PEREZ SANZ", **extra[1]),
    )
