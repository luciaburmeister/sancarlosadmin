import json
from django.utils.translation import gettext_lazy as _
from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget, Widget
from configurator.models import ConfigurationPreset, ProductTemplate
from company.models import Company


class JSONWidget(Widget):
    """Round-trip JSON: export as JSON string, import by parsing it back."""

    def render(self, value, obj=None, **kwargs):
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False)

    def clean(self, value, row=None, **kwargs):
        if not value or str(value).strip() == "":
            return {}
        if isinstance(value, dict):
            return value
        return json.loads(value)


class ConfigurationPresetResource(resources.ModelResource):
    product = fields.Field(
        column_name=_("Product"),
        attribute="product",
        widget=ForeignKeyWidget(ProductTemplate, "name"),
    )
    company = fields.Field(
        column_name=_("Company"),
        attribute="company",
        widget=ForeignKeyWidget(Company, "name"),
    )
    parameter_values = fields.Field(
        column_name=_("Parameter Values"),
        attribute="parameter_values",
        widget=JSONWidget(),
    )

    class Meta:
        model = ConfigurationPreset
        fields = (
            "id",
            "reference_code",
            "name",
            "product",
            "company",
            "parameter_values",
            "is_active",
            "order",
        )
        export_order = fields
        import_id_fields = ("id",)
