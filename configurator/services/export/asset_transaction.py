from typing import Any, Dict

from core.text import join_unique

from .base import BaseExportStrategy
from .context_helpers import get_company_context


class AssetTransactionExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting an AssetTransaction (albarán de entrega/movimiento).
    """
    def __init__(self, transaction):
        super().__init__(transaction)
        self.transaction = transaction

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        asset = self.transaction.asset
        client = asset.client if asset else None

        identifiers = []
        if asset:
            for ident in asset.identifiers.all():
                identifiers.append({
                    "label": ident.label,
                    "value": ident.value,
                })

        project_file = self.transaction.project_file
        working_zones = []
        if project_file:
            working_zones = [
                {
                    "name": zone.name,
                    "zone_type": zone.zone_type,
                    "zone_type_display": zone.get_zone_type_display(),
                    "street_address": zone.street_address,
                    "city": zone.city,
                    "postal_code": zone.postal_code,
                    "province": zone.province,
                    "country": zone.country.code if zone.country else "",
                    "full_address": zone.full_address,
                    "cnae": zone.cnae,
                    "nima": zone.nima,
                }
                for zone in project_file.working_zones.all()
            ]

        context = {
            "transaction": self.transaction,
            "asset": asset,
            "asset_name": asset.name if asset else None,
            "asset_type": str(asset.asset_type) if asset and asset.asset_type else None,
            "asset_data": (asset.data or {}) if asset else {},
            "identifiers": identifiers,
            "transaction_type": self.transaction.get_transaction_type_display(),
            "transaction_type_raw": self.transaction.transaction_type,
            "from_zone": str(self.transaction.from_zone) if self.transaction.from_zone else None,
            "to_zone": str(self.transaction.to_zone) if self.transaction.to_zone else None,
            "date": self.transaction.date,
            "notes": self.transaction.notes or "",
            "data": self.transaction.data or {},
            "firma": self.transaction.firma or "",
            "firma_image": (
                f"data:image/png;base64,{self.transaction.firma}"
                if self.transaction.firma else ""
            ),
            "has_firma": bool(self.transaction.firma),
            "performed_by": str(self.transaction.performed_by) if self.transaction.performed_by else None,
            "client": client,
            "client_name": str(client) if client else None,
            "client_phone": client.phone if client else "",
            "client_tax_id": client.tax_identifier if client else "",
            "client_nima": client.nima if client else "",
            "project_file": project_file,
            "project_file_reference": project_file.reference if project_file else None,
            "project_file_name": project_file.name if project_file else None,
            "project_file_client": (
                str(project_file.client) if project_file and project_file.client else None
            ),
            "project_file_working_zones": working_zones,
            "project_file_zone_type": join_unique(
                zone["zone_type_display"] for zone in working_zones
            ),
            "project_file_address": join_unique(zone["full_address"] for zone in working_zones),
            "project_file_postal_code": join_unique(zone["postal_code"] for zone in working_zones),
            "project_file_city": join_unique(zone["city"] for zone in working_zones),
            "project_file_province": join_unique(zone["province"] for zone in working_zones),
            "project_file_cnae": join_unique(zone["cnae"] for zone in working_zones),
            "project_file_nima": join_unique(zone["nima"] for zone in working_zones),
        }

        company_ctx = get_company_context(self.transaction.company)
        context.update(company_ctx)

        return context

    def get_product(self):
        """
        AssetTransactions are not linked to products.
        Return None so templates with product=NULL can be matched.
        """
        return None
