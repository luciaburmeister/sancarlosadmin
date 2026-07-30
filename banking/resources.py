from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget

from company.models import Company
from banking.models import BankTransactionCategory


class BankTransactionCategoryResource(resources.ModelResource):
    company = fields.Field(
        column_name="company",
        attribute="company",
        widget=ForeignKeyWidget(Company, "name"),
    )

    class Meta:
        model = BankTransactionCategory
        fields = ("id", "name", "company")
        export_order = ("id", "name", "company")
        import_id_fields = ("company", "name")
