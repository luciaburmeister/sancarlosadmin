"""
Shared builder for turning a per-type field definition (AssetTypeField,
ProductParameter, ...) into a Django form field.

Both assets.forms.build_asset_data_form_fields and
configurator.forms.configuration_form.ConfigurationForm build one Django
form field per row of a "field/parameter definition" model, dispatching on
a TextChoices value shared across those models ("number"/"text"/"boolean"/
"select", assets adding "date"). DynamicFieldBuilder holds that dispatch
once; callers only supply the bits that differ (text max length, boolean
widget, whether SELECT gets a blank "---------" choice).
"""

from django import forms
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminSingleDateWidget,
)


def options_to_choices(options):
    """Normalize a field's stored `options` (list or dict) into choice pairs."""
    if isinstance(options, list):
        return [(o, o) for o in options]
    if isinstance(options, dict):
        return list(options.items())
    return []


def json_schema_fragment(field_type, *, options=None):
    """
    JSON Schema fragment (`type`/`format`/`enum`) for one field/parameter
    definition's stored value. Mirrors DynamicFieldBuilder's per-type dispatch
    so schema-generating code (AssetType.build_data_schema_from_fields) reads
    the same type semantics instead of re-forking them.
    """
    if field_type == "number":
        return {"type": "number"}
    if field_type == "boolean":
        return {"type": "boolean"}
    fragment = {"type": "string"}
    if field_type == "date":
        fragment["format"] = "date"
    elif field_type == "select":
        fragment["enum"] = options if isinstance(options, list) else []
    return fragment


class DynamicFieldBuilder:
    """
    Builds a Django form field for one field/parameter definition.

    `text_max_length`, `boolean_widget` and `select_blank_choice` capture the
    only real differences between today's two callers; add more knobs here
    rather than re-forking the type dispatch when a third type-driven form
    shows up.
    """

    def __init__(self, *, text_max_length=200, boolean_widget=None, select_blank_choice=True):
        self.text_max_length = text_max_length
        self.boolean_widget = boolean_widget
        self.select_blank_choice = select_blank_choice

    def build(self, *, field_type, label, required, options=None, initial=None):
        builder = getattr(self, f"_build_{field_type}", None)
        if builder is None:
            raise ValueError(f"Unsupported dynamic field type: {field_type!r}")
        return builder(label=label, required=required, options=options, initial=initial)

    def _build_number(self, *, label, required, options, initial):
        return forms.FloatField(
            label=label, required=required, initial=initial,
            widget=UnfoldAdminDecimalFieldWidget(),
        )

    def _build_text(self, *, label, required, options, initial):
        return forms.CharField(
            label=label, required=required, initial=initial,
            max_length=self.text_max_length, widget=UnfoldAdminTextInputWidget(),
        )

    def _build_boolean(self, *, label, required, options, initial):
        if isinstance(initial, str):
            initial = initial.lower() == "true"
        else:
            initial = bool(initial)
        kwargs = dict(label=label, required=False, initial=initial)
        if self.boolean_widget is not None:
            kwargs["widget"] = self.boolean_widget()
        return forms.BooleanField(**kwargs)

    def _build_select(self, *, label, required, options, initial):
        choices = options_to_choices(options)
        if self.select_blank_choice:
            choices = [("", "---------")] + choices
        return forms.ChoiceField(
            label=label, required=required, initial=initial,
            choices=choices, widget=UnfoldAdminSelectWidget(),
        )

    def _build_date(self, *, label, required, options, initial):
        return forms.DateField(
            label=label, required=required, initial=initial,
            widget=UnfoldAdminSingleDateWidget(),
        )
