from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.forms.dynamic_fields import json_schema_fragment


class FieldType(models.TextChoices):
    NUMBER = "number", _("Number")
    TEXT = "text", _("Text")
    BOOLEAN = "boolean", _("Boolean")
    SELECT = "select", _("Select")
    MULTISELECT = "multiselect", _("Multiple select")
    DATE = "date", _("Date")


def normalize_key(key):
    """Same normalization as AssetTypeField so schema keys match data keys."""
    return (key or "").strip().lower().replace(" ", "_").replace("-", "_")


class SchemaFieldBase(models.Model):
    """One property of a metadata schema; its value lives in data[key].

    Concrete subclasses add the FK to whatever owns the schema and a unique
    constraint on (owner, key).
    """

    key = models.SlugField(
        _("Key"),
        max_length=100,
        help_text=_("Key stored in the metadata (e.g. color, weight_kg). Lowercase, no spaces."),
    )
    label = models.CharField(_("Label"), max_length=100)
    type = models.CharField(_("Type"), max_length=20, choices=FieldType.choices, default=FieldType.TEXT)
    options = models.JSONField(
        _("Options"),
        default=list,
        blank=True,
        help_text=_('Choices for select fields, e.g. ["RAL9016", "RAL7016"]'),
    )
    required = models.BooleanField(_("Required"), default=False)
    unit = models.CharField(_("Unit"), max_length=20, blank=True, help_text=_("e.g. kg, mm"))
    min_value = models.FloatField(_("Minimum"), null=True, blank=True)
    max_value = models.FloatField(_("Maximum"), null=True, blank=True)
    help_text = models.CharField(_("Help text"), max_length=255, blank=True)
    order = models.PositiveIntegerField(_("Order"), default=0, db_index=True)

    class Meta:
        abstract = True
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.label} ({self.key})"

    @property
    def display_label(self):
        return f"{self.label} ({self.unit})" if self.unit else self.label

    def clean(self):
        super().clean()
        self.key = normalize_key(self.key)
        has_choices = self.type in (FieldType.SELECT, FieldType.MULTISELECT)
        if has_choices and not (isinstance(self.options, list) and self.options):
            raise ValidationError({"options": _("Select fields need a list of options.")})
        if self.type != FieldType.NUMBER and (self.min_value is not None or self.max_value is not None):
            raise ValidationError({"min_value": _("Only number fields can have a minimum or maximum.")})
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValidationError({"max_value": _("The maximum must not be lower than the minimum.")})

    def save(self, *args, **kwargs):
        self.key = normalize_key(self.key)
        super().save(*args, **kwargs)

    def json_schema_fragment(self):
        fragment = {"title": self.label, **json_schema_fragment(self.type, options=self.options)}
        if self.min_value is not None:
            fragment["minimum"] = self.min_value
        if self.max_value is not None:
            fragment["maximum"] = self.max_value
        if self.unit:
            fragment["x-unit"] = self.unit
        if self.help_text:
            fragment["description"] = self.help_text
        return fragment
