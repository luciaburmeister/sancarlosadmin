"""
Product fields the ERP publishes for the storefront, as metafields of each
item's Shopify variant in the ERP's namespace
(``variant.metafields.polipus.<key>``). On the variant, not the product: a
product can hold several items, each with its own fields.

Each ``ProductFieldProvider`` supplies one field from ERP data; adding a
source of product information means adding a provider to
``ShopifyProductFields.PROVIDERS``. The catalog push writes the fields with
the rest of the item and deletes those that no longer have a value.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.shopify.models import ShopifyStore
from sales.services.external_order import ExternalOrderService

if TYPE_CHECKING:
    from inventory.models import InventoryItem


@dataclass(frozen=True)
class ProductField:
    key: str
    value: object | None  # JSON-serialisable; None removes the field from the variant

    @property
    def json(self) -> str:
        return json.dumps(self.value, ensure_ascii=False, sort_keys=True, default=str)


class ProductFieldProvider(ABC):
    KEY: str

    @abstractmethod
    def value(self, store: ShopifyStore, item: InventoryItem) -> object | None:
        """The field's value for the item in this store, or None when it has none."""


class SpecificationsProvider(ProductFieldProvider):
    """The item's properties from its metadata schema (item schemas), in the schema's order.

    ``[{"key", "label", "type", "unit", "value"}]``; empty values are left out,
    and so is the store's category field, which becomes the product type.
    """

    KEY = "specifications"

    def value(self, store: ShopifyStore, item: InventoryItem) -> list[dict] | None:
        if item.metadata_schema_id is None:
            return None
        metadata = item.metadata or {}
        specifications = [
            {"key": field.key, "label": field.label, "type": field.type, "unit": field.unit, "value": metadata[field.key]}
            for field in item.metadata_schema.effective_fields()
            if metadata.get(field.key) not in (None, "", []) and field.key != store.category_field
        ]
        return specifications or None


class SaleModeProvider(ProductFieldProvider):
    """How the store sells the item: an ``inventory.choices.OnlineSaleMode`` value.

    Per variant, as one product's variants can sell differently; the product's
    tags only sum them up.
    """

    KEY = "sale_mode"

    def value(self, store: ShopifyStore, item: InventoryItem) -> str:
        return store.sale_mode(item)


class MadeToMeasureFormProvider(ProductFieldProvider):
    """The parameters a customer chooses for a made-to-measure item: its preset's open parameters.

    ``{"fixed": [{"label", "value"}], "parameters": [{"name", "label", "type", "options", "default"}]}``,
    only while the store takes made-to-measure requests.
    """

    KEY = "made_to_measure"

    def value(self, store: ShopifyStore, item: InventoryItem) -> dict | None:
        if not store.takes_made_to_measure:
            return None
        preset = ExternalOrderService.made_to_measure_preset(item)
        if preset is None:
            return None
        labels = {parameter.name: parameter.label for parameter in preset.product.parameters.all()}
        return {
            "fixed": [{"label": labels.get(name, name), "value": value} for name, value in preset.parameter_values.items()],
            "parameters": [
                {
                    "name": parameter.name,
                    "label": parameter.label,
                    "type": parameter.type,
                    "options": [str(option) for option in parameter.options or []],
                    "default": parameter.default_value or "",
                }
                for parameter in preset.get_missing_parameters()
            ],
        }


class ShopifyProductFields:
    PROVIDERS: tuple[ProductFieldProvider, ...] = (SpecificationsProvider(), SaleModeProvider(), MadeToMeasureFormProvider())
    METAFIELD_TYPE = "json"

    @classmethod
    def for_item(cls, store: ShopifyStore, item: InventoryItem) -> list[ProductField]:
        return [ProductField(provider.KEY, provider.value(store, item)) for provider in cls.PROVIDERS]

    @staticmethod
    def fingerprint(fields: list[ProductField]) -> str:
        """What identifies the fields' content; empty when none has a value."""
        if all(field.value is None for field in fields):
            return ""
        content = "|".join(f"{field.key}={field.json}" for field in fields)
        return hashlib.sha256(content.encode()).hexdigest()
