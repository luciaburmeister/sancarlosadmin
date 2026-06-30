from import_export import fields, resources
from django.utils.translation import gettext_lazy as _

from management.models import ProjectFile
from sales.models import Client


class ProjectFileResource(resources.ModelResource):
    reference = fields.Field(column_name=_("Reference"), attribute="reference")
    name = fields.Field(column_name=_("Project Name"), attribute="name")
    client_name = fields.Field(column_name=_("Client"))
    client_tax_id = fields.Field(column_name=_("Client NIF/CIF"))
    status = fields.Field(column_name=_("Status"))
    total_amount_value = fields.Field(column_name=_("Invoiceable (subtotal)"))
    total_invoiced_value = fields.Field(column_name=_("Invoiced (subtotal)"))
    remaining_value = fields.Field(column_name=_("Remaining (subtotal)"))
    currency = fields.Field(column_name=_("Currency"))
    percentage_invoiced = fields.Field(
        column_name=_("% Invoiced"), attribute="percentage_invoiced"
    )
    start_date = fields.Field(column_name=_("Start Date"), attribute="start_date")
    end_date = fields.Field(column_name=_("End Date"), attribute="end_date")
    company = fields.Field(column_name=_("Company"))

    class Meta:
        model = ProjectFile
        fields = (
            "reference",
            "name",
            "client_name",
            "client_tax_id",
            "status",
            "total_amount_value",
            "total_invoiced_value",
            "remaining_value",
            "currency",
            "percentage_invoiced",
            "start_date",
            "end_date",
            "company",
        )
        export_order = fields

    def dehydrate_client_name(self, pf):
        return pf.client.name if pf.client else ""

    def dehydrate_client_tax_id(self, pf):
        return getattr(pf.client, "tax_identifier", "") if pf.client else ""

    def dehydrate_status(self, pf):
        return pf.get_status_display()

    def dehydrate_total_amount_value(self, pf):
        return pf.total_amount.amount if pf.total_amount else 0

    def dehydrate_total_invoiced_value(self, pf):
        return pf.total_invoiced.amount if pf.total_invoiced else 0

    def dehydrate_remaining_value(self, pf):
        invoiceable = pf.total_amount.amount if pf.total_amount else 0
        invoiced = pf.total_invoiced.amount if pf.total_invoiced else 0
        return invoiceable - invoiced

    def dehydrate_currency(self, pf):
        return str(pf.total_amount.currency) if pf.total_amount else "EUR"

    def dehydrate_company(self, pf):
        return pf.company.name if pf.company else ""
