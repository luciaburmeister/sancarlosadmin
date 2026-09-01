from import_export import resources, fields
from django.utils.translation import gettext_lazy as _

from purchasing.models import PurchaseOrder


class PurchaseOrderResource(resources.ModelResource):
    """
    Read-only resource for exporting Purchase Orders — the committed-spend view,
    pivotable by cost center. Actual (invoiced) spend comes from InvoiceResource.
    """

    number = fields.Field(column_name=_("PO Number"), attribute="number", readonly=True)
    date_ordered = fields.Field(column_name=_("Date Ordered"), attribute="date_ordered", readonly=True)
    provider = fields.Field(column_name=_("Supplier"), readonly=True)
    po_type = fields.Field(column_name=_("Type"), readonly=True)
    cost_center_code = fields.Field(column_name=_("Cost Center Code"), readonly=True)
    cost_center = fields.Field(column_name=_("Cost Center"), readonly=True)
    project_file = fields.Field(column_name=_("Project File"), readonly=True)
    status = fields.Field(column_name=_("Status"), readonly=True)
    total_amount_value = fields.Field(column_name=_("Total Amount"), readonly=True)
    currency = fields.Field(column_name=_("Currency"), readonly=True)
    company = fields.Field(column_name=_("Company"), readonly=True)

    COLUMN_FORMATS = {
        "total_amount_value": "#,##0.00",
    }

    class Meta:
        model = PurchaseOrder
        fields = (
            "id",
            "number",
            "date_ordered",
            "provider",
            "po_type",
            "cost_center_code",
            "cost_center",
            "project_file",
            "status",
            "total_amount_value",
            "currency",
            "company",
        )
        export_order = fields

    def dehydrate_provider(self, po):
        return po.provider.name if po.provider_id else ""

    def dehydrate_po_type(self, po):
        return po.get_po_type_display()

    def dehydrate_cost_center_code(self, po):
        return po.cost_center.code if po.cost_center_id else ""

    def dehydrate_cost_center(self, po):
        # Full path, so a pivot can group on any ancestor without a join.
        return po.cost_center.full_name if po.cost_center_id else ""

    def dehydrate_project_file(self, po):
        return str(po.project_file) if po.project_file_id else ""

    def dehydrate_status(self, po):
        return po.get_status_display()

    def dehydrate_total_amount_value(self, po):
        return po.total_amount.amount if po.total_amount else 0

    def dehydrate_currency(self, po):
        return str(po.total_amount.currency) if po.total_amount else ""

    def dehydrate_company(self, po):
        return po.company.name if po.company_id else ""
