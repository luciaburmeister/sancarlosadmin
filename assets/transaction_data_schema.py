"""
Typed field schema for AssetTransaction.data, keyed by transaction_type.

One class per family (mirrors the mobile app's per-type strategy classes in
api/fleet/views/assets.py and attendance.py): its field set, how to pre-fill
from existing `data`, and how to fold cleaned form values back into `data`.
RETURN has no schema -- it keeps the raw JSON editor.
"""
import datetime as _datetime

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from unfold.fields import UnfoldAdminAutocompleteModelChoiceField
from unfold.widgets import (
    UnfoldAdminAutocompleteModelChoiceFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextInputWidget,
)

from company.models import ConfigurationList, WorkingZone
from sales.models import Client

from .models import Asset, CubaFillState, FormAffirmative, PorteAction, TransactionTypeChoices

_EMPTY_CHOICE = ("", _("Select…"))


def select_field(label, choices, required=False):
    return forms.ChoiceField(label=label, choices=choices, required=required, widget=UnfoldAdminSelectWidget)


def model_select_field(label, queryset, required=False, help_text=""):
    return forms.ModelChoiceField(
        label=label, queryset=queryset, required=required, help_text=help_text,
        widget=UnfoldAdminSelectWidget,
    )


class SelectedOnlyAutocompleteWidget(UnfoldAdminAutocompleteModelChoiceFieldWidget):
    """Unfold's template only prints the selected <option>, but Select.optgroups
    still walks (and str()s) the whole queryset. Fetch just the selected rows,
    like django.contrib.admin's AutocompleteMixin; select2 loads the rest."""

    def optgroups(self, name, value, attrs=None):
        field = self.choices.field
        selected = {str(v) for v in value if str(v) not in field.empty_values}
        options = []
        if selected:
            try:
                objs = list(self.choices.queryset.filter(pk__in=selected))
            except (ValueError, ValidationError):  # legacy non-pk value in data
                objs = []
            for index, obj in enumerate(objs):
                options.append(self.create_option(name, obj.pk, field.label_from_instance(obj), True, index))
        return [(None, options, 0)]


def autocomplete_field(label, queryset, url_path, required=False):
    """`queryset` still bounds what validates on save; `url_path` only feeds
    the search dropdown."""
    return UnfoldAdminAutocompleteModelChoiceField(
        url_path=url_path, label=label, queryset=queryset, required=required,
        widget=SelectedOnlyAutocompleteWidget,
    )


def text_field(label, placeholder=None):
    attrs = {"placeholder": placeholder} if placeholder else {}
    return forms.CharField(label=label, required=False, widget=UnfoldAdminTextInputWidget(attrs=attrs))


def config_choices(company, key):
    """Live per-company options from ConfigurationList, or None if no row
    exists yet for this key (caller falls back to free text)."""
    if company is None:
        return None
    config = ConfigurationList.objects.filter(company=company, key=key).first()
    if not config or not config.values:
        return None
    return [_EMPTY_CHOICE] + [(v, v) for v in config.values]


def config_or_text_field(company, key, label):
    """ConfigurationList-backed dropdown if a row exists for (company, key),
    otherwise a plain text field -- upgrades to a dropdown automatically the
    moment someone adds that config row."""
    choices = config_choices(company, key)
    if choices is not None:
        return select_field(label, choices)
    return text_field(label, placeholder=_("No preset list yet — type a value"))


def match_choice_value(field, raw_value):
    """Case-insensitively match `raw_value` against a ChoiceField's choices,
    returning the choice's actual value, or "" if nothing matches."""
    if not raw_value:
        return raw_value
    if isinstance(field, forms.ChoiceField):
        for choice_value, _label in field.choices:
            if choice_value and str(choice_value).lower() == str(raw_value).lower():
                return choice_value
        return ""
    return raw_value


class TransactionFieldSchema:
    """One typed `data` schema per transaction_type family."""

    transaction_types = ()
    field_names = ()

    @classmethod
    def fields(cls, company):
        raise NotImplementedError

    @classmethod
    def initial(cls, data, company=None):
        raise NotImplementedError

    @classmethod
    def build(cls, transaction_type, cleaned, existing=None, tx_date=None):
        raise NotImplementedError

    @classmethod
    def conditional_js(cls):
        return " || ".join(f"transaction_type == '{t}'" for t in cls.transaction_types)


class CubaSchema(TransactionFieldSchema):
    transaction_types = (TransactionTypeChoices.MOVEMENT, TransactionTypeChoices.DELIVERY)
    field_names = (
        "tipo_cuba", "cubaEstado", "vasAVerter", "m3", "trabajo",
        "matricula", "peso_vertido", "vertedero", "implemento", "tipo_camion",
        "reference_number",
    )

    @classmethod
    def fields(cls, company):
        vertedero_qs = WorkingZone.objects.none()
        if company is not None:
            vertedero_qs = WorkingZone.objects.filter(
                company=company, zone_type=WorkingZone.ZoneType.VERTEDERO
            ).select_related("company").order_by("name")
        return {
            "tipo_cuba": config_or_text_field(company, "materiales", _("Container material")),
            "cubaEstado": select_field(_("Container state"), [_EMPTY_CHOICE] + list(CubaFillState.choices)),
            "vasAVerter": select_field(_("Going to be dumped?"), [_EMPTY_CHOICE] + list(FormAffirmative.choices)),
            "m3": config_or_text_field(company, "m3", _("m³")),
            "trabajo": config_or_text_field(company, "trabajos", _("Work type")),
            "matricula": text_field(_("Plate"), placeholder=_("e.g. 0709CBB")),
            "peso_vertido": text_field(_("Weight Dumped (kg)"), placeholder=_("e.g. 500")),
            "vertedero": model_select_field(
                _("Landfill"), vertedero_qs,
                help_text=_("Stored as the zone's name, same as the mobile app's field."),
            ),
            "implemento": config_or_text_field(company, "implemento", _("Implement")),
            "tipo_camion": config_or_text_field(company, "tipo_camion", _("Truck type")),
            "reference_number": text_field(_("Reference number"), placeholder=_("Customer/order reference, if any")),
        }

    @classmethod
    def initial(cls, data, company=None):
        data = data or {}
        initial = {name: data.get(name, "") for name in cls.field_names if name != "vertedero"}
        vertedero_name = data.get("vertedero")
        zone = None
        if vertedero_name and company is not None:
            zone = WorkingZone.objects.filter(
                company=company, zone_type=WorkingZone.ZoneType.VERTEDERO, name=vertedero_name,
            ).first()
        initial["vertedero"] = zone.pk if zone else None
        return initial

    @classmethod
    def build(cls, transaction_type, cleaned, existing=None, tx_date=None):
        merged = dict(existing or {})
        for name in cls.field_names:
            if name == "vertedero" or name not in cleaned:
                continue
            merged[name] = cleaned.get(name) or ""
        if "vertedero" in cleaned:
            zone = cleaned.get("vertedero")
            merged["vertedero"] = zone.name if zone else ""
        return merged


class PorteSchema(TransactionFieldSchema):
    transaction_types = (
        TransactionTypeChoices.PORTE_MOVEMENT,
        TransactionTypeChoices.PORTE_LOAD,
        TransactionTypeChoices.PORTE_DELIVERY,
    )
    # "action" isn't a field -- it's 1:1 with transaction_type
    # (PORTE_ACTION_TRANSACTION_TYPES, assets/models.py), so build() derives it.
    field_names = (
        "matricula", "load_description", "client_id", "machine_id",
        "goods_type", "tonnes_transported", "package_count", "reference_number",
    )
    ACTION_BY_TYPE = {
        TransactionTypeChoices.PORTE_MOVEMENT: PorteAction.RECOGIDA,
        TransactionTypeChoices.PORTE_LOAD: PorteAction.CARGA,
        TransactionTypeChoices.PORTE_DELIVERY: PorteAction.ENTREGA,
    }

    @classmethod
    def fields(cls, company):
        client_qs = Client.objects.filter(company=company).order_by("name") if company else Client.objects.none()
        machine_qs = Asset.objects.filter(company=company).select_related("asset_type").order_by("name") if company else Asset.objects.none()
        return {
            "matricula": text_field(_("Plate"), placeholder=_("e.g. 0709CBB")),
            "load_description": text_field(_("Load description"), placeholder=_("e.g. zahorra")),
            "client_id": autocomplete_field(_("Client"), client_qs, "admin:client_autocomplete"),
            "machine_id": autocomplete_field(_("Machine"), machine_qs, "admin:asset_autocomplete"),
            "goods_type": config_or_text_field(company, "goods_type", _("Goods type")),
            "tonnes_transported": text_field(_("Tonnes transported"), placeholder=_("e.g. 12.5")),
            "package_count": text_field(_("Number of packages"), placeholder=_("Only if transported as packages")),
            "reference_number": text_field(_("Reference number"), placeholder=_("Customer/order reference, if any")),
        }

    @classmethod
    def initial(cls, data, company=None):
        data = data or {}
        return {name: data.get(name) for name in cls.field_names}

    @classmethod
    def build(cls, transaction_type, cleaned, existing=None, tx_date=None):
        merged = dict(existing or {})
        for name in ("matricula", "load_description", "goods_type", "tonnes_transported", "package_count", "reference_number"):
            if name in cleaned:
                merged[name] = cleaned.get(name) or ""
        for name in ("client_id", "machine_id"):
            if name in cleaned:
                related = cleaned.get(name)
                merged[name] = related.pk if related else None
        merged["movement_type"] = "porte"
        merged["action"] = cls.ACTION_BY_TYPE[transaction_type]
        return merged


class ReceiptSchema(TransactionFieldSchema):
    transaction_types = (TransactionTypeChoices.RECEIPT,)
    # nro_albaran is also a real field on AssetTransactionReceipt (kept in sync).
    field_names = ("nro_albaran", "material_vertido", "reference_number")

    @classmethod
    def fields(cls, company):
        return {
            "nro_albaran": text_field(_("Albaran Number"), placeholder=_("Type the albaran number, e.g. 01083485")),
            "material_vertido": cls.material_vertido_field(company),
            "reference_number": text_field(_("Reference number"), placeholder=_("Customer/order reference, if any")),
        }

    @classmethod
    def material_vertido_field(cls, company):
        """Shared with AssetTransactionReceiptAdminForm, which edits the same
        key on a different model (AssetTransactionReceipt.data)."""
        return config_or_text_field(company, "material_vertido", _("Material Dumped"))

    @classmethod
    def initial(cls, data, company=None):
        data = data or {}
        return {name: data.get(name, "") for name in cls.field_names}

    @classmethod
    def build(cls, transaction_type, cleaned, existing=None, tx_date=None):
        merged = dict(existing or {})
        for name in cls.field_names:
            if name in cleaned:
                merged[name] = cleaned.get(name) or ""
        return merged


class HoursSchema(TransactionFieldSchema):
    transaction_types = (TransactionTypeChoices.MACHINE_HOURS, TransactionTypeChoices.LABORER_HOURS)
    # data is an interval list built by start/pause/resume/end, not a flat
    # shape -- a capped set of start/end rows covers the common case.
    KEY_PREFIX = {
        TransactionTypeChoices.MACHINE_HOURS: "machine",
        TransactionTypeChoices.LABORER_HOURS: "laborer",
    }
    ROWS = 4
    field_names = tuple(
        name
        for i in range(1, ROWS + 1)
        for name in (f"interval_{i}_start", f"interval_{i}_end")
    )

    @classmethod
    def fields(cls, company):
        from .forms import TimeChoiceField  # lazy: assets.forms imports this module

        fields = {}
        for i in range(1, cls.ROWS + 1):
            fields[f"interval_{i}_start"] = TimeChoiceField(label=_("Interval %(n)d start") % {"n": i})
            fields[f"interval_{i}_end"] = TimeChoiceField(label=_("Interval %(n)d end") % {"n": i})
        return fields

    @classmethod
    def initial(cls, data, company=None):
        intervals = (data or {}).get("intervals") or []
        initial = {}
        for i in range(1, cls.ROWS + 1):
            interval = intervals[i - 1] if i - 1 < len(intervals) else {}
            initial[f"interval_{i}_start"] = cls._time_from_str(interval.get("start"))
            initial[f"interval_{i}_end"] = cls._time_from_str(interval.get("end"))
        return initial

    @classmethod
    def build(cls, transaction_type, cleaned, existing=None, tx_date=None):
        merged = dict(existing or {})
        prefix = cls.KEY_PREFIX[transaction_type]
        intervals = []
        for i in range(1, cls.ROWS + 1):
            start = cleaned.get(f"interval_{i}_start")
            end = cleaned.get(f"interval_{i}_end")
            if start is None and end is None:
                continue
            intervals.append({
                "start": start.strftime("%H:%M:%S") if start else None,
                "end": end.strftime("%H:%M:%S") if end else None,
            })
        merged["intervals"] = intervals

        tx_date = tx_date or timezone.now()
        base_date = (timezone.localtime(tx_date) if timezone.is_aware(tx_date) else tx_date).date()
        if intervals and intervals[0]["start"]:
            start_time = _datetime.datetime.strptime(intervals[0]["start"], "%H:%M:%S").time()
            merged[f"{prefix}_start_time"] = _datetime.datetime.combine(base_date, start_time).isoformat()
        if intervals and intervals[-1]["end"]:
            end_time = _datetime.datetime.strptime(intervals[-1]["end"], "%H:%M:%S").time()
            merged[f"{prefix}_end_time"] = _datetime.datetime.combine(base_date, end_time).isoformat()
        else:
            merged.pop(f"{prefix}_end_time", None)
        return merged

    @staticmethod
    def _time_from_str(value):
        if not value:
            return None
        try:
            return _datetime.datetime.strptime(value[:8], "%H:%M:%S").time()
        except ValueError:
            return None


SCHEMAS = (CubaSchema, PorteSchema, ReceiptSchema, HoursSchema)
_SCHEMA_BY_TYPE = {t: schema for schema in SCHEMAS for t in schema.transaction_types}

# Unfold ModelAdmin.conditional_fields format: {field_name: JS boolean expression}.
# A field name shared across schemas (e.g. "matricula" in Cuba+Porte,
# "reference_number" in Cuba+Porte+Receipt) must OR its schemas' conditions
# together -- a plain dict comprehension would silently let the last schema
# to define that name clobber the others, hiding the field for every family
# but the last one.
TRANSACTION_DATA_CONDITIONAL_FIELDS = {}
for _schema in SCHEMAS:
    for _name in _schema.field_names:
        _condition = _schema.conditional_js()
        _existing = TRANSACTION_DATA_CONDITIONAL_FIELDS.get(_name)
        TRANSACTION_DATA_CONDITIONAL_FIELDS[_name] = (
            f"({_existing}) || ({_condition})" if _existing else _condition
        )

ALL_SCHEMA_FIELD_NAMES = tuple(name for schema in SCHEMAS for name in schema.field_names)
ALL_FAMILIES = [t for schema in SCHEMAS for t in schema.transaction_types]


def data_schema_fields(transaction_type, company):
    """{field_name: form_field} for the schema of one transaction_type. Empty
    dict for types with no typed schema (they keep the raw `data` editor)."""
    schema = _SCHEMA_BY_TYPE.get(transaction_type)
    return schema.fields(company) if schema else {}


def initial_from_data(transaction_type, data, company=None):
    """{field_name: initial value} to pre-fill a schema form from an existing data dict."""
    schema = _SCHEMA_BY_TYPE.get(transaction_type)
    return schema.initial(data, company=company) if schema else {}


def build_data_from_cleaned(transaction_type, cleaned, existing_data=None, tx_date=None):
    """Merge typed cleaned-field values into existing_data (or a fresh dict),
    preserving any keys the schema doesn't know about instead of clobbering them."""
    schema = _SCHEMA_BY_TYPE.get(transaction_type)
    if not schema:
        return dict(existing_data or {})
    return schema.build(transaction_type, cleaned, existing=existing_data, tx_date=tx_date)
