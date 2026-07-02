from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.models import ContentType
from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget, Widget
from inventory.models import InventoryItem, Warehouse
from company.models import Company
from materials.models import Provider


class ContentTypeWidget(Widget):
    """Import/export ContentType as 'app_label.model' string."""

    def render(self, value, obj=None, **kwargs):
        if value is None:
            return ""
        return f"{value.app_label}.{value.model}"

    def clean(self, value, row=None, **kwargs):
        if not value or str(value).strip() == "":
            return None
        app_label, model = str(value).strip().split(".")
        return ContentType.objects.get(app_label=app_label, model=model)


class MoneyAmountWidget(Widget):
    """Renders / cleans the numeric amount of a MoneyField."""

    def render(self, value, obj=None, **kwargs):
        if value is None:
            return ""
        # value is a Money instance; return just the numeric part
        return str(value.amount) if hasattr(value, "amount") else str(value)

    def clean(self, value, row=None, **kwargs):
        if value is None or str(value).strip() == "":
            return None
        from decimal import Decimal
        return Decimal(str(value).strip())


class MoneyCurrencyWidget(Widget):
    """Renders / cleans the currency code of a MoneyField."""

    def __init__(self, default_currency="EUR"):
        self.default_currency = default_currency

    def render(self, value, obj=None, **kwargs):
        if value is None:
            return self.default_currency
        # value is a Money instance; return the currency code
        if hasattr(value, "currency"):
            return str(value.currency)
        return str(value) if value else self.default_currency

    def clean(self, value, row=None, **kwargs):
        if value is None or str(value).strip() == "":
            return self.default_currency
        return str(value).strip().upper()


class InventoryItemResource(resources.ModelResource):
    company = fields.Field(
        column_name=_("Company"),
        attribute="company",
        widget=ForeignKeyWidget(Company, "name"),
    )
    preferred_supplier = fields.Field(
        column_name=_("Preferred Supplier"),
        attribute="preferred_supplier",
        widget=ForeignKeyWidget(Provider, "name"),
    )
    default_warehouse = fields.Field(
        column_name=_("Warehouse"),
        attribute="default_warehouse",
        widget=ForeignKeyWidget(Warehouse, "code"),
    )
    product_content_type = fields.Field(
        column_name=_("Product Content Type"),
        attribute="product_content_type",
        widget=ContentTypeWidget(),
    )

    # ── Money fields split into amount + currency ─────────────────────────
    selling_price = fields.Field(
        column_name=_("Selling Price"),
        attribute="selling_price",
        widget=MoneyAmountWidget(),
    )
    selling_price_currency = fields.Field(
        column_name=_("Selling Price Currency"),
        attribute="selling_price_currency",
        widget=MoneyCurrencyWidget(default_currency="EUR"),
    )
    cost_price = fields.Field(
        column_name=_("Cost Price"),
        attribute="cost_price",
        widget=MoneyAmountWidget(),
    )
    cost_price_currency = fields.Field(
        column_name=_("Cost Price Currency"),
        attribute="cost_price_currency",
        widget=MoneyCurrencyWidget(default_currency="EUR"),
    )

    class Meta:
        model = InventoryItem
        fields = (
            "id",
            "code",
            "name",
            "description",
            "item_type",
            "company",
            "preferred_supplier",
            "default_warehouse",
            "product_content_type",
            "product_object_id",
            "selling_price",
            "selling_price_currency",
            "cost_price",
            "cost_price_currency",
            "min_stock",
        )
        export_order = fields
        import_id_fields = ("id",)
