"""
The DjangoQL schema used by every advanced-search admin in this project.

Plain DjangoQL introspection walks the whole model graph, which here means
every related model - and every one of its columns - becomes searchable and
shows up in the completion popup. This schema instead exposes exactly what each
app declared in the registry, under localized names.

To make an admin searchable:

1. Declare the model's searchable fields in `<app>/djangoql_fields.py` - or, for
   an app with several models, in a module inside `<app>/djangoql_fields/`::

       from general.djangoql import SearchFields, has_value, register

       @register(Widget)
       class WidgetSearchFields(SearchFields):
           fields = ("id", "name", "created_at", "company",
                     has_value("has_serial", "serial_number"))

   Declarations are imported automatically and shared by every schema: declare
   a model once and any search that can reach it benefits.

2. Add `DjangoQLSearchAdminMixin` (from `general.djangoql.admin`) to the
   ModelAdmin, before `ModelAdmin` itself.

3. Add the Spanish name of any new field to `locale/es/LC_MESSAGES/django.po`
   under the "djangoql field" context, then run `compilemessages`. Names
   without a translation simply stay in English.

   Those msgids are built at runtime, so `makemessages` cannot extract them and
   will mark the whole block obsolete: if it ever runs, restore the block.
   `TranslationCoverageTest` fails when an exposed name loses its entry.
"""

import pkgutil
from importlib import import_module

from django.apps import apps
from django.db.models import ManyToManyRel, ManyToOneRel
from django.db.models.fields.related import ForeignObjectRel
from django.utils.module_loading import module_has_submodule
from djangoql.exceptions import DjangoQLSchemaError
from djangoql.schema import DjangoQLField, DjangoQLSchema

from .fields import (
    FIELD_CLASS_MAP,
    RelationIsNullField,
    TranslatedRelationField,
    TranslatedUnknownField,
)
from .registry import is_registered, registered_fields

#: Per-app module (or package) holding the `@register` declarations.
DECLARATIONS_MODULE = "djangoql_fields"

_autodiscovered = False


def autodiscover():
    """
    Import every app's field declarations, once.

    An app can use either `djangoql_fields.py` or a `djangoql_fields/` package,
    in which case every module inside it is imported too - so one file per
    model needs no `__init__` boilerplate.
    """
    global _autodiscovered
    if _autodiscovered:
        return
    _autodiscovered = True
    from . import defaults  # noqa: F401  (declarations for models we don't own)

    for app_config in apps.get_app_configs():
        if not module_has_submodule(app_config.module, DECLARATIONS_MODULE):
            continue
        module_name = "%s.%s" % (app_config.name, DECLARATIONS_MODULE)
        module = import_module(module_name)
        for _, name, _ in pkgutil.iter_modules(getattr(module, "__path__", [])):
            import_module("%s.%s" % (module_name, name))


class RegisteredDjangoQLSchema(DjangoQLSchema):
    """
    Search schema built from the registry, exposing localized field names.

    Subclass and override `field_specs()` for a search that needs to narrow or
    extend what a model declared.
    """

    def __init__(self, model):
        autodiscover()
        if not is_registered(model):
            raise DjangoQLSchemaError(
                "%s is not available for DjangoQL search: declare its fields "
                "in %s.%s"
                % (
                    model.__name__,
                    model._meta.app_label,
                    DECLARATIONS_MODULE,
                )
            )
        super().__init__(model)

    def excluded(self, model):
        return not is_registered(model)

    def field_specs(self, model):
        return registered_fields(model)

    def get_fields(self, model):
        fields = []
        taken = set()
        for spec in self.field_specs(model):
            field = self.build_field(model, spec)
            if field is None:
                continue
            if field.name in taken:
                # Two fields translating to the same name would silently
                # shadow each other; fall back to the ORM name.
                if field.lookup_name in taken:
                    continue
                field.name = field.lookup_name
            taken.add(field.name)
            fields.append(field)
        return fields

    def build_field(self, model, spec):
        if isinstance(spec, type) and issubclass(spec, DjangoQLField):
            field = spec(model=model)
        else:
            field = self.build_model_field(model, spec)
        if field is not None:
            field.schema = self
        return field

    def build_model_field(self, model, name):
        field = model._meta.get_field(name)
        kwargs = {"model": model, "lookup_name": name}
        if isinstance(field, (ManyToOneRel, ManyToManyRel, ForeignObjectRel)):
            kwargs["nullable"] = True
        else:
            kwargs["nullable"] = field.null

        if field.is_relation:
            if not field.related_model or self.excluded(field.related_model):
                return None
            kwargs["related_model"] = field.related_model
            return TranslatedRelationField(**kwargs)

        # Only choice fields get completion options: options for a plain text
        # field are fetched with an unfiltered `Model.objects` query, which
        # would leak values across companies (see CompanyRestrictedAdmin).
        kwargs["suggest_options"] = bool(getattr(field, "choices", None))
        field_cls = FIELD_CLASS_MAP.get(
            self.get_field_cls(field), TranslatedUnknownField
        )
        return field_cls(**kwargs)

    def lookup_path(self, path):
        """Translate a path of localized relation names into ORM names."""
        model = self.model_label(self.current_model)
        parts = []
        for part in path:
            field = self.models[model].get(part)
            if field is None or field.type != "relation":
                # Unreachable: validate() resolved this path already.
                return path
            parts.append(field.get_lookup_name())
            model = field.relation
        return parts

    def resolve_name(self, name):
        field = super().resolve_name(name)
        if field is None:
            # The expression ends on a relation, so it can only be compared to
            # None. Return a stand-in that knows that relation's ORM name.
            field = self.resolve_trailing_relation(name)
        return field

    def resolve_trailing_relation(self, name):
        model = self.model_label(self.current_model)
        relation = None
        for part in name.parts:
            relation = self.models[model].get(part)
            if relation is None:
                return None
            model = relation.relation
        field = RelationIsNullField(
            model=relation.model,
            lookup_name=relation.get_lookup_name(),
        )
        field.name = relation.name
        field.schema = self
        return field
