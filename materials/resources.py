from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget
from .models import Material, Category, Provider
from company.models import Company
from django.utils.translation import gettext_lazy as _


class MaterialResource(resources.ModelResource):
    category = fields.Field(
        column_name="category",
        attribute="category",
        widget=ForeignKeyWidget(Category, "name"),
    )
    provider = fields.Field(
        column_name="provider",
        attribute="provider",
        widget=ForeignKeyWidget(Provider, "name"),
    )
    company = fields.Field(
        column_name="company",
        attribute="company",
        widget=ForeignKeyWidget(Company, "name"),
    )
    cost_per_unit_value = fields.Field(column_name="cost_per_unit_value")
    currency = fields.Field(column_name="currency")

    class Meta:
        model = Material
        fields = (
            "reference_code",
            "name",
            "rule_key",
            "category",
            "unit_type",
            "cost_per_unit_value",
            "currency",
            "provider",
            "company",
        )
        export_order = fields
        import_id_fields = ("reference_code",)

    def dehydrate_cost_per_unit_value(self, material):
        return material.cost_per_unit.amount if material.cost_per_unit else 0

    def dehydrate_currency(self, material):
        return str(material.cost_per_unit.currency) if material.cost_per_unit else "EUR"
