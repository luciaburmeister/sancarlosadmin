from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget, DecimalWidget, CharWidget
from .models import Material, Category, Provider
from company.models import Company
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money
from decimal import Decimal, InvalidOperation


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
    # Explicit fields for the MoneyField's two DB columns so preview works
    cost_per_unit = fields.Field(
        column_name="cost_per_unit",
        attribute="cost_per_unit",
        widget=DecimalWidget(),
    )
    cost_per_unit_currency = fields.Field(
        column_name="cost_per_unit_currency",
        attribute="cost_per_unit_currency",
        widget=CharWidget(),
    )
    # Virtual export-only fields for the split cost columns
    cost_per_unit_value = fields.Field(column_name="cost_per_unit_value", readonly=True)
    currency = fields.Field(column_name="currency", readonly=True)

    class Meta:
        model = Material
        fields = (
            "reference_code",
            "name",
            "rule_key",
            "category",
            "unit_type",
            "cost_per_unit",
            "cost_per_unit_currency",
            "cost_per_unit_value",
            "currency",
            "provider",
            "company",
        )
        export_order = (
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
        import_id_fields = ("reference_code",)

    def before_import_row(self, row, **kwargs):
        """Map the split cost columns back to the MoneyField DB columns."""
        # Prefer cost_per_unit_value (export format), fall back to cost_per_unit
        cost = row.get("cost_per_unit_value") or row.get("cost_per_unit")
        curr = row.get("currency") or row.get("cost_per_unit_currency") or "EUR"

        if cost is not None:
            row["cost_per_unit"] = str(cost)
            row["cost_per_unit_currency"] = str(curr)

    def before_save_instance(self, instance, row, **kwargs):
        """
        Construct a proper Money object for the MoneyField.
        django-import-export sets the raw DB columns but MoneyField needs
        a Money object for the descriptor to work correctly.
        """
        cost = row.get("cost_per_unit")
        curr = row.get("cost_per_unit_currency") or "EUR"

        if cost is not None:
            try:
                amount = Decimal(str(cost))
            except (InvalidOperation, ValueError):
                amount = Decimal("0")
            instance.cost_per_unit = Money(amount, str(curr))

    def dehydrate_cost_per_unit_value(self, material):
        return material.cost_per_unit.amount if material.cost_per_unit else 0

    def dehydrate_currency(self, material):
        return str(material.cost_per_unit.currency) if material.cost_per_unit else "EUR"
