from django.utils.translation import gettext_lazy as _
from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget

from assets.models import Asset, AssetTransaction
from company.models import Company, WorkingZone
from management.models import ProjectFile


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
