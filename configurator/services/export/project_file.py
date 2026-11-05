from typing import Any, Dict

from .base import BaseExportStrategy
from .context_helpers import get_company_context


class ProjectFileExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a ProjectFile as a repair order (orden de reparación).

    Exposes the expediente, its client, the linked asset (vehicle) with its
    identifiers and type-defined data fields, and the latest reception
    configuration (product with exclude_from_invoicing) if any, including
    any photos taken of the asset during that specific reception.
    """

    def __init__(self, project_file):
        super().__init__(project_file)
        self.project_file = project_file

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        pf = self.project_file
        client = pf.client
        asset = pf.asset

        identifiers = []
        asset_fields = []
        if asset:
            identifiers = [
                {"label": ident.label, "value": ident.value}
                for ident in asset.identifiers.all()
            ]
            data = asset.data or {}
            asset_fields = [
                {"label": spec.label, "value": data.get(spec.name)}
                for spec in asset.asset_type.fields.all()
            ]

        reception = None
        reception_fields = []
        reception_data = {}
        reception_photos = []
        reception_config = (
            pf.direct_configurations.filter(product__exclude_from_invoicing=True)
            .order_by("-created_at")
            .first()
        )
        if reception_config:
            reception = reception_config
            for pv in reception_config.param_values.select_related("parameter").order_by("order"):
                value = pv.parameter.normalize_value(pv.value)
                reception_fields.append({"label": pv.parameter.label, "value": value})
                reception_data[pv.parameter.name] = value
            reception_photos = [
                {"url": photo.photo.url} for photo in reception_config.photos.all()
            ]

        context = {
            "project_file": pf,
            "reference": pf.reference,
            "project_file_name": pf.name,
            "status": pf.get_status_display(),
            "description": pf.description or "",
            "date": pf.start_date or (pf.created_at.date() if pf.created_at else None),
            "created_at": pf.created_at,
            "end_date": pf.end_date,
            # Client block
            "client": client,
            "client_name": str(client) if client else None,
            "client_tax_id": getattr(client, "tax_identifier", "") if client else "",
            "client_address": getattr(client, "address", "") if client else "",
            "client_phone": getattr(client, "phone", "") if client else "",
            "client_email": getattr(client, "email", "") if client else "",
            # Asset (vehicle) block
            "asset": asset,
            "asset_name": asset.name if asset else None,
            "asset_type": str(asset.asset_type) if asset else None,
            "asset_data": (asset.data or {}) if asset else {},
            "identifiers": identifiers,
            "asset_fields": asset_fields,
            # Reception block
            "reception": reception,
            "reception_fields": reception_fields,
            "reception_data": reception_data,
            "reception_notes": reception.notes if reception else "",
            "reception_date": reception.created_at if reception else None,
            "reception_photos": reception_photos,
        }

        context.update(get_company_context(pf.company))
        return context

    def get_product(self):
        """
        Project files are not linked to a single product.
        Return None so templates with product=NULL can be matched.
        """
        return None
