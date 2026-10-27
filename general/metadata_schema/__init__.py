"""
Typed, admin-editable structure for a JSON ``metadata`` column.

An app declares a concrete subclass of `models.SchemaFieldBase` (one row per
property: key, label, type, options, bounds…), turns a set of those rows into
a JSON Schema with `build_json_schema`, and checks stored data against it
with `validate_data`. The data itself stays a single JSONB column, so it can
carry a GIN index instead of one table per property.

`models` is not imported here: it defines a Django model and must only load
once the app registry is ready (import it from `general.metadata_schema.models`).
"""

from .barcode import validate_barcode
from .schema import build_json_schema, validate_data

__all__ = ["build_json_schema", "validate_barcode", "validate_data"]
