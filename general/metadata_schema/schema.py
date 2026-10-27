from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from jsonschema import Draft202012Validator, FormatChecker


def build_json_schema(fields):
    """JSON Schema for an object holding one property per field definition.

    ``fields`` are `SchemaFieldBase` rows. Unknown keys are allowed so data
    written under an earlier version of the schema survives an edit to it.
    """
    properties = {}
    required = []
    for field in fields:
        properties[field.key] = field.json_schema_fragment()
        if field.required:
            required.append(field.key)
    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = required
    return schema


def validate_data(schema, data):
    """Raise a ValidationError listing every way ``data`` breaks ``schema``.

    Errors are keyed by the top-level property they concern (``__all__`` for
    the object itself, e.g. a missing required key is reported under that
    key), so a form can show each one next to its field.
    """
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = {}
    for error in validator.iter_errors(data):
        if error.validator == "required" and not error.path:
            missing = [key for key in schema.get("required", []) if key not in (data or {})]
            for key in missing:
                errors.setdefault(key, []).append(_("This field is required."))
            continue
        key = str(error.path[0]) if error.path else "__all__"
        errors.setdefault(key, []).append(error.message)
    if errors:
        raise ValidationError(errors)
