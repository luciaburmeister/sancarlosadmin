from django.utils.translation import gettext_lazy as _
from import_export import resources, fields
from configurator.models import Configuration, ConfigurationItem


class SummarizedConfigurationResource(resources.ModelResource):
    items_summary = fields.Field(column_name=_("Items Summary"))

    class Meta:
        model = Configuration
        fields = (
            "id",
            "name",
            "client__name",
            "product__name",
            "status",
            "subtotal_price",
            "total_price",
            "company__name",
            "created_at",
            "items_summary",
        )
        export_order = fields

    def dehydrate_items_summary(self, configuration):
        items = configuration.items.all()
        return "\n".join([f"{item.label} (Qty: {item.quantity})" for item in items])


class DetailedItemsResource(resources.ModelResource):
    configuration_name = fields.Field(attribute="configuration__name", column_name=_("Configuration"))
    client = fields.Field(attribute="configuration__client__name", column_name=_("Client"))
    product = fields.Field(attribute="configuration__product__name", column_name=_("Product"))
    component_name = fields.Field(attribute="component__name", column_name=_("Component"))
    
    quantity = fields.Field(attribute="quantity", column_name=_("Quantity"))
    length = fields.Field(attribute="length", column_name=_("Length"))
    
    unit_price_amount = fields.Field(column_name=_("Unit Price Amount"))
    unit_price_currency = fields.Field(column_name=_("Unit Price Currency"))
    line_total_amount = fields.Field(column_name=_("Line Total Amount"))
    line_total_currency = fields.Field(column_name=_("Line Total Currency"))

    class Meta:
        model = ConfigurationItem
        fields = (
            "configuration_name",
            "client",
            "product",
            "component_name",
            "quantity",
            "length",
            "unit_price_amount",
            "unit_price_currency",
            "line_total_amount",
            "line_total_currency",
            "note",
        )
        export_order = fields

    def export(self, queryset=None, *args, **kwargs):
        """
        If we receive a Configuration queryset, expand it to its line items.
        Also apply eager loading to prevent N+1 queries.
        """
        if queryset is not None and queryset.model == Configuration:
            queryset = ConfigurationItem.objects.filter(configuration__in=queryset)
        
        if queryset is not None:
            queryset = queryset.select_related(
                "component",
                "configuration",
                "configuration__client",
                "configuration__product",
            )
            
        return super().export(queryset, *args, **kwargs)

    def dehydrate_unit_price_amount(self, item):
        return item.unit_price.amount

    def dehydrate_unit_price_currency(self, item):
        return str(item.unit_price.currency)

    def dehydrate_line_total_amount(self, item):
        return item.line_total.amount

    def dehydrate_line_total_currency(self, item):
        return str(item.line_total.currency)
