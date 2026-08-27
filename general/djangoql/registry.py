"""
Registry of what each model exposes to DjangoQL search.

A model is searchable - as the subject of a search, or as a relation reached
from one - only once it is registered here, the same way a model shows up in
the admin only once a ModelAdmin is registered:

    # <app>/djangoql_fields.py, or a module inside <app>/djangoql_fields/
    from general.djangoql import SearchFields, register

    @register(Widget)
    class WidgetSearchFields(SearchFields):
        fields = ("id", "name", "created_at", "company")

Declarations are imported automatically (see `schema.autodiscover`).
"""

from django.core.exceptions import ImproperlyConfigured

_REGISTRY = {}


class SearchFields:
    """
    What one model exposes to DjangoQL search.

    `fields` is a tuple of ORM field names (`"invoice_number"`, `"provider"`,
    `"items"` - reverse relations included) and/or flags built with `flag()`,
    `has_value()` or `is_set()`.

    Only listed fields are searchable and offered as completions, and a listed
    relation is dropped unless its target model is registered too.
    """

    fields = ()

    @classmethod
    def get_fields(cls):
        return tuple(cls.fields)


def register(model):
    """Class decorator registering a `SearchFields` subclass for `model`."""

    def decorator(declaration):
        if not (
            isinstance(declaration, type) and issubclass(declaration, SearchFields)
        ):
            raise ImproperlyConfigured(
                "@register(%s) must decorate a SearchFields subclass, got %r"
                % (model.__name__, declaration)
            )
        if not declaration.get_fields():
            raise ImproperlyConfigured(
                "%s declares no DjangoQL fields for %s"
                % (declaration.__name__, model.__name__)
            )
        registered = _REGISTRY.get(model)
        if registered is not None and registered is not declaration:
            raise ImproperlyConfigured(
                "%s is already registered for DjangoQL search by %s"
                % (model.__name__, registered.__name__)
            )
        _REGISTRY[model] = declaration
        return declaration

    return decorator


def registered_fields(model):
    """Return the declared field specs for `model`, or () if not registered."""
    declaration = _REGISTRY.get(model)
    return declaration.get_fields() if declaration else ()


def is_registered(model):
    return model in _REGISTRY


def registered_models():
    return tuple(_REGISTRY)
