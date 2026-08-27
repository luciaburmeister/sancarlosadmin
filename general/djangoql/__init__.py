"""
Localized, whitelisted DjangoQL search.

Apps declare what each of their models exposes in `<app>/djangoql_fields.py`,
and admins opt in with `general.djangoql.admin.DjangoQLSearchAdminMixin`. See
`schema.py` for the full how-to.

`admin` is deliberately not re-exported here so model modules can import
`flag`, `register` & co. without pulling in django.contrib.admin.
"""

from .fields import (
    ComputedBoolField,
    flag,
    has_value,
    is_set,
    localized_name,
    prefix_lookups,
)
from .registry import SearchFields, register, registered_fields
from .schema import RegisteredDjangoQLSchema

__all__ = [
    "ComputedBoolField",
    "RegisteredDjangoQLSchema",
    "SearchFields",
    "flag",
    "has_value",
    "is_set",
    "localized_name",
    "prefix_lookups",
    "register",
    "registered_fields",
]
