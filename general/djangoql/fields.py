"""
Schema fields for localized DjangoQL search.

The DjangoQL frontend shows - and its parser reads - the field names a schema
exposes, so the only way to offer the search language in another language is to
expose translated names and map them back to ORM lookups. Every public name
goes through `localized_name()`, which translates the ORM field name under the
"djangoql field" context.
"""

import re

from django.core.exceptions import FieldDoesNotExist
from django.db.models import F, Q
from django.utils.translation import pgettext
from djangoql.exceptions import DjangoQLSchemaError
from djangoql.schema import (
    BoolField,
    DateField,
    DateTimeField,
    DjangoQLField,
    FloatField,
    IntField,
    RelationField,
    StrField,
)

# Field names are translated under this context so short, identifier-like
# msgids ("due_date", "provider", ...) can't collide with unrelated strings.
TRANSLATION_CONTEXT = "djangoql field"

IDENTIFIER_RE = re.compile(r"^[_A-Za-z][_0-9A-Za-z]*$")

# Words the DjangoQL lexer reads as operators / literals, so they can never be
# used as a field name.
RESERVED_WORDS = frozenset(
    ("or", "and", "not", "in", "startswith", "endswith", "True", "False", "None")
)


def localized_name(name):
    """
    Return the translated, lexer-safe public name for an ORM field name.

    The lexer only accepts ASCII identifiers, so a translation with accents,
    spaces or a reserved word would make the field unreachable - those fall
    back to the untranslated name instead of breaking search.
    """
    translated = pgettext(TRANSLATION_CONTEXT, name)
    if translated == name:
        return name
    if not IDENTIFIER_RE.match(translated) or translated in RESERVED_WORDS:
        return name
    return translated


def prefix_lookups(q, path):
    """
    Rewrite every lookup in `q` so it resolves from `path` instead.

    Both sides are rewritten: the lookup itself and any `F()` on the right,
    so a flag comparing two columns still compares them on the related model.
    """
    if not path:
        return q
    prefix = "__".join(path)
    children = [
        prefix_lookups(child, path)
        if isinstance(child, Q)
        else ("%s__%s" % (prefix, child[0]), _prefix_value(child[1], prefix))
        for child in q.children
    ]
    return Q(*children, _connector=q.connector, _negated=q.negated)


def _prefix_value(value, prefix):
    if isinstance(value, F):
        return F("%s__%s" % (prefix, value.name))
    return value


class TranslatedFieldMixin:
    """
    Exposes a field under a localized name while keeping the ORM name for
    lookups, choices and completion options.
    """

    # ORM name of the underlying field, and the msgid of the public name.
    lookup_name = None

    def __init__(self, lookup_name=None, **kwargs):
        self.lookup_name = lookup_name or kwargs.pop("name", None) or self.name
        self.schema = None
        kwargs["name"] = localized_name(self.lookup_name)
        super().__init__(**kwargs)

    def get_lookup_name(self):
        return self.lookup_name

    def _field_choices(self):
        # Upstream looks the field up by `self.name`, which here is localized.
        if not self.model:
            return []
        try:
            return self.model._meta.get_field(self.lookup_name).choices or []
        except (AttributeError, FieldDoesNotExist):
            return []

    def get_lookup(self, path, operator, value):
        # `path` holds the localized names typed by the user; translate it back
        # to ORM names before building the lookup.
        return super().get_lookup(self.orm_path(path), operator, value)

    def orm_path(self, path):
        if not path or self.schema is None:
            return path
        return self.schema.lookup_path(path)


class TranslatedStrField(TranslatedFieldMixin, StrField):
    def get_options(self, search):
        # Choices first (as upstream does), then distinct stored values.
        choice_options = DjangoQLField.get_options(self, search)
        if choice_options:
            return choice_options
        lookup = {}
        if search:
            lookup["%s__icontains" % self.lookup_name] = search
        return (
            self.model.objects.filter(**lookup)
            .order_by(self.lookup_name)
            .values_list(self.lookup_name, flat=True)
            .distinct()
        )


class TranslatedIntField(TranslatedFieldMixin, IntField):
    pass


class TranslatedFloatField(TranslatedFieldMixin, FloatField):
    pass


class TranslatedBoolField(TranslatedFieldMixin, BoolField):
    pass


class TranslatedDateField(TranslatedFieldMixin, DateField):
    pass


class TranslatedDateTimeField(TranslatedFieldMixin, DateTimeField):
    pass


class TranslatedRelationField(TranslatedFieldMixin, RelationField):
    pass


class TranslatedUnknownField(TranslatedFieldMixin, DjangoQLField):
    pass


#: Upstream field class -> its localized counterpart.
FIELD_CLASS_MAP = {
    StrField: TranslatedStrField,
    IntField: TranslatedIntField,
    FloatField: TranslatedFloatField,
    BoolField: TranslatedBoolField,
    DateField: TranslatedDateField,
    DateTimeField: TranslatedDateTimeField,
}


class RelationIsNullField(TranslatedFieldMixin, DjangoQLField):
    """
    Stands in for an expression that ends on a relation (`provider = None`).
    Upstream builds that lookup straight from the typed name, which here is the
    localized one, so the schema hands this back to keep the ORM path.
    """

    type = "relation"
    nullable = True

    def validate(self, value):
        if value is not None:
            raise DjangoQLSchemaError(
                "Related model %s can be compared to None only, but not to %s"
                % (self.name, type(value).__name__)
            )


class ComputedBoolField(TranslatedFieldMixin, BoolField):
    """
    A boolean flag backed by a Q expression instead of a stored column, for the
    questions people actually ask ("is it overdue?") that no single column
    answers. Subclasses set `name` and implement `build_q()`; see `flag()`.
    """

    name = None  # also the msgid of the localized name
    nullable = False

    def build_q(self):
        raise NotImplementedError

    def get_lookup(self, path, operator, value):
        if operator not in ("=", "!="):
            raise DjangoQLSchemaError(
                'Field "%s" is a flag: it can only be used with "=" or "!=" '
                "and True / False" % self.name
            )
        orm_path = self.orm_path(path)
        q = prefix_lookups(self.build_q(), orm_path)
        if (value is False) != (operator == "!="):
            # A negated flag read through a relation would otherwise also match
            # rows that have no related row at all (the join is NULL, so the
            # condition it negates is never true). "This provider has no NIF"
            # should not include invoices with no provider.
            q = ~q
            if orm_path:
                q &= Q(**{"%s__isnull" % "__".join(orm_path): False})
        return q


def flag(name, build_q, doc=None):
    """
    Declare a boolean search flag.

    `build_q` is a callable returning a Q object relative to the model the flag
    is registered on - a callable rather than a Q so that things like
    `timezone.localdate()` are evaluated per query, not at import time.

        IS_OVERDUE = flag(
            "is_overdue",
            lambda: Q(due_date__lt=timezone.localdate()) & ~Q(payment_status="paid"),
        )
    """
    return type(
        str(name),
        (ComputedBoolField,),
        {"name": name, "build_q": staticmethod(build_q), "__doc__": doc},
    )


def has_value(name, field_name, doc=None):
    """Flag: a char / text column is filled in."""
    return flag(
        name,
        lambda: ~Q(**{field_name: ""}) & Q(**{"%s__isnull" % field_name: False}),
        doc or "True when %s is set." % field_name,
    )


def is_set(name, field_name, doc=None):
    """Flag: a nullable column or relation is not empty."""
    return flag(
        name,
        lambda: Q(**{"%s__isnull" % field_name: False}),
        doc or "True when %s is set." % field_name,
    )
