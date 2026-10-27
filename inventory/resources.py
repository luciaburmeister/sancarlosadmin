from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.models import ContentType
from import_export import resources, fields
from import_export.widgets import DecimalWidget, ForeignKeyWidget, IntegerWidget, JSONWidget, Widget
from general.metadata_schema import validate_barcode, validate_data
from inventory.models import InventoryItem, ItemSchema, Warehouse
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


class CompanyScopedSchemaWidget(ForeignKeyWidget):
    """ItemSchema by name, looked up in the row's company (names repeat across companies)."""

    def __init__(self):
        super().__init__(ItemSchema, "name")

    def get_queryset(self, value, row, *args, **kwargs):
        company_name = (row or {}).get(str(_("Company")))
        queryset = ItemSchema.objects.all()
        return queryset.filter(company__name=company_name) if company_name else queryset


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
    metadata_schema = fields.Field(
        column_name=_("Metadata schema"),
        attribute="metadata_schema",
        widget=CompanyScopedSchemaWidget(),
    )
    metadata = fields.Field(
        column_name=_("Metadata"),
        attribute="metadata",
        widget=JSONWidget(),
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

    # Derived from the movement ledger; the admin export queryset carries the
    # stock_on_hand_total annotation, so this costs no query per row.
    stock_on_hand = fields.Field(
        column_name=_("Stock on Hand"),
        attribute="stock_on_hand",
        widget=IntegerWidget(),
        readonly=True,
    )
    incoming_quantity = fields.Field(
        column_name=_("Incoming"),
        attribute="incoming_quantity",
        widget=DecimalWidget(),
        readonly=True,
    )
    reserved_quantity = fields.Field(
        column_name=_("Reserved"),
        attribute="reserved_quantity",
        widget=DecimalWidget(),
        readonly=True,
    )

    class Meta:
        model = InventoryItem
        fields = (
            "id",
            "code",
            "barcode",
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
            "metadata_schema",
            "metadata",
            "stock_on_hand",
            "incoming_quantity",
            "reserved_quantity",
        )
        export_order = fields
        import_id_fields = ("id",)

    def validate_instance(self, instance, import_validation_errors=None, validate_unique=True):
        # clean_model_instances stays off (older rows would fail other model
        # rules), so the barcode and metadata checks run here explicitly.
        errors = dict(import_validation_errors or {})
        instance.metadata = instance.metadata or {}
        try:
            validate_barcode(instance.barcode)
        except ValidationError as exc:
            errors["barcode"] = exc.messages
        schema = instance.metadata_schema
        if schema is not None:
            try:
                validate_data(schema.json_schema(), instance.metadata)
            except ValidationError as exc:
                errors["metadata"] = [f"{k}: {m}" for k, ms in exc.message_dict.items() for m in ms]
        super().validate_instance(instance, errors, validate_unique)
