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
            "cost_price",
            "min_stock",
        )
        export_order = fields
        import_id_fields = ("id",)
