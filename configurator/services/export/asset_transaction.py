from typing import Any, Dict
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

        # Gather asset identifiers
        identifiers = []
        for ident in asset.identifiers.all():
            identifiers.append({
                "label": ident.label,
                "value": ident.value,
            })

        context = {
            "transaction": self.transaction,
            "asset": asset,
            "asset_name": asset.name,
            "asset_type": str(asset.asset_type) if asset.asset_type else None,
            "asset_data": asset.data or {},
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
            "client": asset.client,
            "client_name": str(asset.client) if asset.client else None,
            "project_file": self.transaction.project_file,
            "project_file_reference": (
                self.transaction.project_file.reference
                if self.transaction.project_file else None
            ),
            "project_file_name": (
                self.transaction.project_file.name
                if self.transaction.project_file else None
            ),
            "project_file_client": (
                str(self.transaction.project_file.client)
                if self.transaction.project_file and self.transaction.project_file.client
                else None
            ),
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
