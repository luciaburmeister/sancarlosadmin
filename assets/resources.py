from django.utils.translation import gettext_lazy as _
from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget

from assets.models import Asset, AssetTransaction, AssetType
from company.models import Company, WorkingZone, Employee
from management.models import ProjectFile
from materials.models import Provider
from sales.models import Client


class AssetResource(resources.ModelResource):
    company = fields.Field(
        column_name=_("Company"),
        attribute="company",
        widget=ForeignKeyWidget(Company, "name"),
    )
    asset_type = fields.Field(
        column_name=_("Asset Type"),
        attribute="asset_type",
        widget=ForeignKeyWidget(AssetType, "name"),
    )
    client = fields.Field(
        column_name=_("Client"),
        attribute="client",
        widget=ForeignKeyWidget(Client, "name"),
    )
    provider = fields.Field(
        column_name=_("Provider"),
        attribute="provider",
        widget=ForeignKeyWidget(Provider, "name"),
    )
    assigned_to = fields.Field(
        column_name=_("Assigned To"),
        attribute="assigned_to",
        widget=ForeignKeyWidget(Employee, "identification_number"),
    )
    current_working_zone = fields.Field(
        column_name=_("Current Working Zone"),
        attribute="current_working_zone",
        widget=ForeignKeyWidget(WorkingZone, "name"),
    )
    zone_client = fields.Field(column_name=_("Zone Client"), readonly=True)

    class Meta:
        model = Asset
        fields = (
            "id",
            "name",
            "asset_type",
            "client",
            "current_working_zone",
            "zone_client",
            "provider",
            "assigned_to",
            "company",
            "is_active",
            "description",
        )
        export_order = fields
        import_id_fields = ("id",)

    def dehydrate_zone_client(self, asset):
        if asset.current_working_zone and asset.current_working_zone.client:
            return asset.current_working_zone.client.name
        return ""


class AssetTransactionResource(resources.ModelResource):
    company = fields.Field(
        column_name=_("Company"),
        attribute="company",
        widget=ForeignKeyWidget(Company, "name"),
    )
    asset = fields.Field(
        column_name=_("Asset"),
        attribute="asset",
        widget=ForeignKeyWidget(Asset, "name"),
    )
    from_zone = fields.Field(
        column_name=_("From Zone"),
        attribute="from_zone",
        widget=ForeignKeyWidget(WorkingZone, "name"),
    )
    to_zone = fields.Field(
        column_name=_("To Zone"),
        attribute="to_zone",
        widget=ForeignKeyWidget(WorkingZone, "name"),
    )
    project_file = fields.Field(
        column_name=_("Project File"),
        attribute="project_file",
        widget=ForeignKeyWidget(ProjectFile, "reference"),
    )

    class Meta:
        model = AssetTransaction
        fields = (
            "id",
            "company",
            "asset",
            "transaction_type",
            "from_zone",
            "to_zone",
            "project_file",
            "date",
            "notes",
        )
        export_order = fields
        import_id_fields = ("id",)
