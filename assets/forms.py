import datetime
from datetime import timedelta

from django import forms
from django.utils.translation import gettext_lazy as _
from unfold.fields import UnfoldAdminAutocompleteModelChoiceField
from unfold.widgets import (
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminSelect2MultipleWidget,
    UnfoldAdminTextareaWidget,
    UnfoldBooleanSwitchWidget,
)

from assets.constants import MAX_RECEIPT_SPLIT_PARTS


# Aligns with the schedule's 15-minute slots; covers a normal working day.
_DURATION_PRESET_MINUTES = (
    15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 240, 300, 360, 480,
)


def _duration_label(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return _("%(hours)dh %(mins)d min") % {"hours": hours, "mins": mins}
    if hours == 1:
        return _("1 hour")
    if hours:
        return _("%(hours)d hours") % {"hours": hours}
    return _("%(mins)d min") % {"mins": mins}


def _duration_choices(extra_minutes=None):
    minutes_set = set(_DURATION_PRESET_MINUTES)
    if extra_minutes and extra_minutes > 0:
        minutes_set.add(int(extra_minutes))
    return [("", "---------")] + [
        (m, _duration_label(m)) for m in sorted(minutes_set)
    ]


class EstimatedDurationChoiceField(forms.TypedChoiceField):
    """Select of common durations instead of a raw hh:mm:ss DurationField input."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("choices", _duration_choices())
        kwargs.setdefault("coerce", self._coerce)
        kwargs.setdefault("empty_value", None)
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", UnfoldAdminSelectWidget)
        super().__init__(*args, **kwargs)

    @staticmethod
    def _coerce(value):
        if value in ("", None):
            return None
        # has_changed() runs _coerce on both the submitted data (raw minutes
        # from the select) AND the field's initial value — which is already
        # a timedelta straight off the model instance, not a choice string.
        if isinstance(value, timedelta):
            return value
        return timedelta(minutes=int(value))

    def prepare_value(self, value):
        if isinstance(value, timedelta):
            minutes = int(value.total_seconds() // 60)
            if minutes > 0 and minutes not in dict(self.choices):
                self.choices = _duration_choices(extra_minutes=minutes)
            return minutes if minutes > 0 else ""
        return value


# Matches TransportAssigner's WORKING_DAY_START/END/SLOT_STEP
# (assets/services/transport_assigner.py) — the hours it can actually search
# a free slot within.
_TIME_SLOT_START_MINUTES = 7 * 60
_TIME_SLOT_END_MINUTES = 17 * 60
_TIME_SLOT_STEP_MINUTES = 15


def _time_choices(extra_minutes=None):
    minutes_set = set(range(_TIME_SLOT_START_MINUTES, _TIME_SLOT_END_MINUTES + 1, _TIME_SLOT_STEP_MINUTES))
    if extra_minutes is not None and extra_minutes >= 0:
        minutes_set.add(int(extra_minutes))
    return [("", "---------")] + [
        (m, "%02d:%02d" % (m // 60, m % 60)) for m in sorted(minutes_set)
    ]


class TimeChoiceField(forms.TypedChoiceField):
    """Select of common start times instead of a raw HH:MM:SS TimeField input."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("choices", _time_choices())
        kwargs.setdefault("coerce", self._coerce)
        kwargs.setdefault("empty_value", None)
        kwargs.setdefault("required", False)
        kwargs.setdefault("widget", UnfoldAdminSelectWidget)
        super().__init__(*args, **kwargs)

    @staticmethod
    def _coerce(value):
        if value in ("", None):
            return None
        # has_changed() runs _coerce on both the submitted data (raw minutes
        # from the select) AND the field's initial value — which is already
        # a time straight off the model instance, not a choice string.
        if isinstance(value, datetime.time):
            return value
        minutes = int(value)
        return datetime.time(minutes // 60, minutes % 60)

    def prepare_value(self, value):
        if isinstance(value, datetime.time):
            minutes = value.hour * 60 + value.minute
            if minutes not in dict(self.choices):
                self.choices = _time_choices(extra_minutes=minutes)
            return minutes
        return value


from company.models import Company, Employee, WorkingZone
from core.admin.company_access import get_user_companies
from core.forms.dynamic_fields import DynamicFieldBuilder
from sales.models import Client

from .constants import SERVICE_TYPE_PLANNING_TYPE
from .models import (
    Asset,
    AssetIdentifier,
    AssetPlanning,
    AssetPlanningRequest,
    AssetType,
    AssetIdentifierLabel,
    AssetTypeCategory,
    CubaFillState,
    CubaTransactionKey,
    FormAffirmative,
    PlanningType,
    PorteTransactionDataKey,
    PlanningUrgency,
    ServiceType,
    TransactionTypeChoices,
)
from .transaction_data_schema import (
    SelectedOnlyAutocompleteWidget,
    autocomplete_field,
    build_data_from_cleaned,
    data_schema_fields,
    match_choice_value,
    text_field,
)


class TruckPlateChoiceField(UnfoldAdminAutocompleteModelChoiceField):
    widget = SelectedOnlyAutocompleteWidget

    def label_from_instance(self, obj):
        plate = obj.matricula or ""
        if plate:
            return f"{plate} — {obj.name}"
        return str(obj)

# Namespaced to Asset specifically: a generic "data__" prefix would eventually
# collide with another model/form using the same convention.
DATA_FIELD_PREFIX = "asset_data__"


def extract_prefixed_dict(cleaned_data, prefix):
    """
    Pull {name: value} entries whose key starts with `prefix`, stripping it
    and serializing date/datetime values so they round-trip through a
    JSONField. Shared by AssetForm.save() and AssetCreateWizardView.done().
    """
    result = {}
    for name, value in cleaned_data.items():
        if not name.startswith(prefix):
            continue
        if isinstance(value, (datetime.date, datetime.datetime)):
            value = value.isoformat()
        result[name[len(prefix):]] = value
    return result

# Assets allow longer free text than configurator parameters and render
# booleans as a switch; see configurator.forms.configuration_form for the
# other DynamicFieldBuilder using this same per-type dispatch.
_asset_field_builder = DynamicFieldBuilder(text_max_length=500, boolean_widget=UnfoldBooleanSwitchWidget)


def build_asset_data_form_fields(asset_type):
    """
    Build declared form fields from an AssetType's field definitions.
    Returns {f"{DATA_FIELD_PREFIX}{name}": form_field} so values round-trip
    to Asset.data.
    """
    fields = {}
    for spec in asset_type.fields.all():
        field = _asset_field_builder.build(
            field_type=spec.type,
            label=spec.label,
            required=spec.required,
            options=spec.options,
        )
        fields[f"{DATA_FIELD_PREFIX}{spec.name}"] = field
    return fields


class AssetForm(forms.ModelForm):
    """
    Change form for assets. AssetAdmin.get_form subclasses it per-object,
    declaring one real form field per AssetTypeField; values live in Asset.data.
    """

    class Meta:
        model = Asset
        exclude = ["data"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        data = self.instance.data or {}
        for name in self.fields:
            if name.startswith(DATA_FIELD_PREFIX):
                self.initial.setdefault(name, data.get(name[len(DATA_FIELD_PREFIX):]))

    def save(self, commit=True):
        obj = super().save(commit=False)
        data = dict(obj.data or {})
        data.update(extract_prefixed_dict(self.cleaned_data, DATA_FIELD_PREFIX))
        obj.data = data
        if commit:
            obj.save()
            self.save_m2m()
        return obj


class AssetPlanningRequestForm(forms.ModelForm):
    """Change/add form for AssetPlanningRequest; swaps raw duration/time inputs for presets."""

    estimated_duration = EstimatedDurationChoiceField(
        label=AssetPlanningRequest._meta.get_field("estimated_duration").verbose_name,
        help_text=AssetPlanningRequest._meta.get_field("estimated_duration").help_text,
        required=True,
    )
    needed_time = TimeChoiceField(
        label=AssetPlanningRequest._meta.get_field("needed_time").verbose_name,
        help_text=AssetPlanningRequest._meta.get_field("needed_time").help_text,
    )

    class Meta:
        model = AssetPlanningRequest
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        # No AssetPlanning for these types, so no job length to estimate.
        if SERVICE_TYPE_PLANNING_TYPE.get(cleaned_data.get("service_type")) == PlanningType.NONE:
            self.errors.pop("estimated_duration", None)
            cleaned_data["estimated_duration"] = None
        return cleaned_data


class AssetAddForm(forms.ModelForm):
    """Minimal add form: type-specific fields are completed on the change form."""

    class Meta:
        model = Asset
        fields = ["name", "asset_type", "client", "company"]


IDENTIFIER_FIELD_PREFIX = "identifier__"


class AssetWizardBasicsForm(forms.Form):
    """Step 1 of the asset creation wizard: pick type, client and company."""

    name = forms.CharField(
        label=_("Name/Label"), max_length=200, widget=UnfoldAdminTextInputWidget()
    )
    asset_type = forms.ModelChoiceField(
        label=_("Asset Type"), queryset=None, widget=UnfoldAdminSelectWidget()
    )
    company = forms.ModelChoiceField(
        label=_("Company"), queryset=None, widget=UnfoldAdminSelectWidget()
    )

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        companies = Company.objects.all()
        if request and not request.user.is_superuser:
            companies = get_user_companies(request.user)

        self.fields["company"].queryset = companies.order_by("name")
        if companies.count() == 1:
            self.fields["company"].initial = companies.first()
        self.fields["asset_type"].queryset = AssetType.objects.filter(
            company__in=companies
        ).select_related("company").order_by("name")
        self.fields["client"] = UnfoldAdminAutocompleteModelChoiceField(
            url_path="admin:client_autocomplete",
            queryset=Client.objects.filter(company__in=companies),
            label=_("Owner/Client"),
            required=False,
        )
        if request and request.method == "GET":
            initial_client = self.initial.get("client")
            initial_pk = getattr(initial_client, "pk", initial_client)
            self.fields["client"].queryset = Client.objects.filter(
                pk=initial_pk
            ) if initial_pk else Client.objects.none()
        self.order_fields(["name", "asset_type", "client", "company"])

    def clean(self):
        cleaned = super().clean()
        company = cleaned.get("company")
        asset_type = cleaned.get("asset_type")
        client = cleaned.get("client")
        if company and asset_type and asset_type.company_id != company.pk:
            self.add_error("asset_type", _("This asset type belongs to another company."))
        if company and client and client.company_id != company.pk:
            self.add_error("client", _("This client belongs to another company."))
        return cleaned


class AssetWizardDetailsForm(forms.Form):
    """Step 2: identifiers and type-specific data fields for the chosen type."""

    class Media:
        # Date fields need the admin calendar machinery; outside a change
        # form the widget's own media lacks its dependencies (same fix as
        # ResourcePlanningFilterForm).
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/core.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
        ]

    def __init__(self, *args, asset_type=None, **kwargs):
        self.asset_type = asset_type
        super().__init__(*args, **kwargs)
        if asset_type is None:
            return
        for label in asset_type.get_identifier_labels():
            field = forms.CharField(
                label=label,
                required=False,
                max_length=100,
                widget=UnfoldAdminTextInputWidget(),
            )
            field.parameter_group = _("Identifiers")
            self.fields[f"{IDENTIFIER_FIELD_PREFIX}{label}"] = field
        for name, field in build_asset_data_form_fields(asset_type).items():
            field.parameter_group = _("%(type)s data") % {"type": asset_type.name}
            self.fields[name] = field

class AssetIdentifierForm(forms.ModelForm):
    class Meta:
        model = AssetIdentifier
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        asset = None
        if self.instance and hasattr(self.instance, 'asset'):
            asset = self.instance.asset
        elif self.initial.get('asset'):
            asset = Asset.objects.filter(pk=self.initial.get('asset')).first()
        
        labels = asset.asset_type.get_identifier_labels() if asset and asset.asset_type_id else []
        if labels:
            self.fields['label'] = forms.ChoiceField(
                choices=[('', '---------')] + [(l, l) for l in labels],
                required=True
            )


class AssetScheduleFilterForm(forms.Form):
    """GET filters for the asset staffing calendar (Unfold Select2 widgets, like Resource Planning)."""

    assigned_to = forms.ModelMultipleChoiceField(
        label=_("Drivers"),
        required=False,
        queryset=Employee.objects.none(),
        widget=UnfoldAdminSelect2MultipleWidget,
    )
    transport_asset = forms.ModelMultipleChoiceField(
        label=_("Trucks"),
        required=False,
        queryset=Asset.objects.none(),
        widget=UnfoldAdminSelect2MultipleWidget,
    )
    service_type = forms.MultipleChoiceField(
        label=_("Service Type"),
        required=False,
        choices=ServiceType.choices,
        widget=UnfoldAdminSelect2MultipleWidget,
    )
    status = forms.MultipleChoiceField(
        label=_("Status"),
        required=False,
        choices=AssetPlanning.STATUS_CHOICES,
        widget=UnfoldAdminSelect2MultipleWidget,
    )
    urgency = forms.MultipleChoiceField(
        label=_("Urgency"),
        required=False,
        choices=PlanningUrgency.choices,
        widget=UnfoldAdminSelect2MultipleWidget,
    )
    planning_type = forms.MultipleChoiceField(
        label=_("Planning Type"),
        required=False,
        choices=AssetPlanning.PLANNING_CHOICES,
        widget=UnfoldAdminSelect2MultipleWidget,
    )
    only_with_plans = forms.BooleanField(
        label=_("Only drivers with plannings"),
        required=False,
        initial=False,
        widget=UnfoldBooleanSwitchWidget,
    )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        employees = Employee.objects.none()
        trucks = Asset.objects.none()
        if company is not None:
            employees = Employee.objects.filter(
                company=company, is_active=True
            ).order_by("first_name", "last_name")
            trucks = Asset.objects.filter(
                company=company,
                is_active=True,
                asset_type__category=AssetTypeCategory.TRUCK,
            ).order_by("name")
        self.fields["assigned_to"].queryset = employees
        self.fields["transport_asset"].queryset = trucks


class AssetMapTableFilterForm(forms.Form):
    """
    GET filters for the AssetMap table view (Unfold Select2 multi-selects,
    like the schedule filters). `zone`/`expediente` choices and the dynamic
    per-AssetTypeField filters depend on which AssetMap is being viewed, so
    they're added in __init__ rather than declared on the class.
    """

    q = forms.CharField(
        label=_("Nombre"),
        required=False,
        widget=UnfoldAdminTextInputWidget(attrs={"placeholder": _("Buscar por nombre…")}),
    )
    sort = forms.ChoiceField(
        label=_("Ordenar por"),
        required=False,
        choices=[
            ("", _("Zona / nombre (por defecto)")),
            ("-zone_changed_at", _("Última fecha de colocación (más reciente primero)")),
            ("zone_changed_at", _("Última fecha de colocación (más antigua primero)")),
        ],
        widget=UnfoldAdminSelectWidget,
    )

    def __init__(self, *args, zone_choices=(), project_file_choices=(), dynamic_field_specs=(), **kwargs):
        super().__init__(*args, **kwargs)
        if zone_choices:
            self.fields["zone"] = forms.MultipleChoiceField(
                label=_("Zona"), required=False, choices=zone_choices,
                widget=UnfoldAdminSelect2MultipleWidget,
            )
        if project_file_choices:
            self.fields["expediente"] = forms.MultipleChoiceField(
                label=_("Expediente"), required=False, choices=project_file_choices,
                widget=UnfoldAdminSelect2MultipleWidget,
            )
        for spec in dynamic_field_specs:
            self.fields[f"df_{spec['key']}"] = forms.MultipleChoiceField(
                label=spec["label"], required=False,
                choices=[(v, v) for v in spec["options"]],
                widget=UnfoldAdminSelect2MultipleWidget,
            )
        self.order_fields(
            ["zone", "expediente", "q"] + [f"df_{spec['key']}" for spec in dynamic_field_specs] + ["sort"]
        )


CUBA_TRANSACTION_TYPE_CHOICES = [
    (TransactionTypeChoices.MOVEMENT, TransactionTypeChoices.MOVEMENT.label),
    (TransactionTypeChoices.DELIVERY, TransactionTypeChoices.DELIVERY.label),
    (TransactionTypeChoices.RECEIPT, TransactionTypeChoices.RECEIPT.label),
]
PORTE_TRANSACTION_TYPE_CHOICES = [
    (TransactionTypeChoices.PORTE_MOVEMENT, TransactionTypeChoices.PORTE_MOVEMENT.label),
    (TransactionTypeChoices.PORTE_LOAD, TransactionTypeChoices.PORTE_LOAD.label),
    (TransactionTypeChoices.PORTE_DELIVERY, TransactionTypeChoices.PORTE_DELIVERY.label),
]


class ManualTransactionForm(forms.Form):

    transaction_type = forms.ChoiceField(
        label=_("Type"),
        choices=CUBA_TRANSACTION_TYPE_CHOICES,
        widget=UnfoldAdminSelectWidget,
    )
    performed_by = forms.ModelChoiceField(
        label=_("Performed by"),
        queryset=Employee.objects.none(),
        required=False,
        widget=UnfoldAdminSelectWidget,
    )
    notes = forms.CharField(
        label=_("Notes (optional)"),
        required=False,
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 2}),
    )
    # A real AssetTransaction field, not one of the per-type data_schema_fields
    # below -- applies to every transaction type, so it's a plain top-level
    # field here too, same treatment as zone/performed_by/notes.
    reference_number = forms.CharField(
        label=_("Reference number"),
        required=False,
        widget=UnfoldAdminTextInputWidget(attrs={"placeholder": _("Customer/order reference, if any")}),
    )

    _MOVEMENT_FIELDS = (
        CubaTransactionKey.ESTADO, CubaTransactionKey.MATERIAL, CubaTransactionKey.VAS_A_VERTER,
        CubaTransactionKey.LANDFILL, CubaTransactionKey.M3, PorteTransactionDataKey.MATRICULA,
    )
    _DELIVERY_FIELDS = (
        CubaTransactionKey.ESTADO, CubaTransactionKey.MATERIAL, CubaTransactionKey.M3,
        PorteTransactionDataKey.MATRICULA,
    )
    _RECEIPT_FIELDS = (CubaTransactionKey.ALBARAN, CubaTransactionKey.MATERIAL_DUMPED)
    _PORTE_LOAD_FIELDS = (
        PorteTransactionDataKey.GOODS_TYPE, PorteTransactionDataKey.TONNES_TRANSPORTED,
        PorteTransactionDataKey.PACKAGE_COUNT,
    )
    _REQUIRES_FULL = frozenset({CubaTransactionKey.MATERIAL, CubaTransactionKey.VAS_A_VERTER})
    _REQUIRES_DUMP = frozenset({CubaTransactionKey.LANDFILL})
    _MOVEMENT_AND_DELIVERY = f"{TransactionTypeChoices.MOVEMENT},{TransactionTypeChoices.DELIVERY}"

    def __init__(self, *args, planning=None, **kwargs):
        self.planning = planning
        super().__init__(*args, **kwargs)
        company = planning.company if planning is not None else None
        is_porte = planning is not None and planning.planning_type == PlanningType.PORTE
        # "" (falsy) renders these ungrouped, above the per-type sections below.
        for name in ("transaction_type", "performed_by", "reference_number", "notes"):
            self.fields[name].parameter_group = ""
        if is_porte:
            self.fields["transaction_type"].choices = PORTE_TRANSACTION_TYPE_CHOICES
        if planning is not None and planning.company_id:
            performed_by = autocomplete_field(
                _("Performed by"),
                Employee.objects.filter(
                    company_id=planning.company_id, is_active=True,
                ).order_by("last_name", "first_name"),
                "admin:employee_autocomplete",
            )
            performed_by.initial = planning.assigned_to_id
            performed_by.parameter_group = ""
            self.fields["performed_by"] = performed_by
            self._add_asset_field()
            if is_porte:
                self._add_porte_fields(company)
            else:
                self._add_cuba_fields(company)
        self.order_fields([
            "transaction_type", "asset", "performed_by", "reference_number", "notes",
        ])

    def _add_asset_field(self):
        planning = self.planning
        if planning.asset_id:
            return
        queryset = Asset.objects.filter(
            company_id=planning.company_id, is_active=True,
        ).exclude(
            asset_type__category__in=(AssetTypeCategory.TRUCK, AssetTypeCategory.MACHINE),
        )
        field = autocomplete_field(
            _("Cuba"), queryset.order_by("name"), "admin:container_autocomplete",
        )
        field.help_text = _("The container this transaction is about. The planning doesn't have one yet.")
        field.parameter_group = ""
        self.fields["asset"] = field

    def _add_plate_field(self, shows_for, group):
        queryset = Asset.objects.filter(
            company_id=self.planning.company_id,
            is_active=True,
            asset_type__category=AssetTypeCategory.TRUCK,
            identifiers__label__iexact=AssetIdentifierLabel.PLATE,
        ).distinct()
        field = TruckPlateChoiceField(
            url_path="admin:truck_plate_autocomplete",
            label=_("Plate"),
            queryset=queryset.order_by("name"),
            required=False,
        )
        self._add_schema_field(PorteTransactionDataKey.MATRICULA, field, shows_for, group)

    def _add_schema_field(self, name, field, shows_for, group, requires=""):
        field.widget.attrs["data-shows-for"] = shows_for
        if requires:
            field.widget.attrs["data-requires"] = requires
        field.parameter_group = group
        self.fields[name] = field

    def _add_cuba_fields(self, company):
        planning = self.planning
        container = data_schema_fields(TransactionTypeChoices.MOVEMENT, company)
        receipt = data_schema_fields(TransactionTypeChoices.RECEIPT, company)
        if not planning.destination_id:
            self.fields["zone"] = forms.ModelChoiceField(
                label=_("Destination zone"),
                queryset=WorkingZone.objects.filter(company_id=planning.company_id).order_by("name"),
                required=False,
                widget=UnfoldAdminSelectWidget,
            )
            self.fields["zone"].widget.attrs["data-shows-for"] = TransactionTypeChoices.DELIVERY
            self.fields["zone"].parameter_group = ""
        group = _("Container Details")
        plate_known = bool(planning.transport_plate())
        m3_known = bool(planning.recorded_capacity())
        for name in self._MOVEMENT_FIELDS:
            if name == PorteTransactionDataKey.MATRICULA and plate_known:
                continue
            if name == CubaTransactionKey.M3 and m3_known:
                continue
            shows_for = (
                TransactionTypeChoices.MOVEMENT
                if name in (CubaTransactionKey.VAS_A_VERTER, CubaTransactionKey.LANDFILL)
                else self._MOVEMENT_AND_DELIVERY
            )
            if name not in self._DELIVERY_FIELDS:
                shows_for = TransactionTypeChoices.MOVEMENT
            if name == PorteTransactionDataKey.MATRICULA:
                self._add_plate_field(shows_for, group)
                continue
            requires = ""
            if name in self._REQUIRES_FULL:
                requires = "full"
            elif name in self._REQUIRES_DUMP:
                requires = "dump"
            self._add_schema_field(name, container[name], shows_for, group, requires)
        if CubaTransactionKey.MATERIAL in self.fields and planning.load_description:
            self.fields[CubaTransactionKey.MATERIAL].initial = match_choice_value(
                self.fields[CubaTransactionKey.MATERIAL], planning.load_description.strip()
            )
        receipt_group = _("Receipt (Verteo) Details")
        for name in self._RECEIPT_FIELDS:
            self._add_schema_field(name, receipt[name], TransactionTypeChoices.RECEIPT, receipt_group)
        self._add_schema_field(
            CubaTransactionKey.NET_WEIGHT,
            text_field(_("Net weight (kg)"), placeholder=_("e.g. 500")),
            TransactionTypeChoices.RECEIPT,
            receipt_group,
        )
        self.fields["receipt_zone"] = forms.ModelChoiceField(
            label=_("Landfill"),
            queryset=WorkingZone.objects.filter(
                company_id=planning.company_id, zone_type=WorkingZone.ZoneType.VERTEDERO,
            ).order_by("name"),
            required=False,
            widget=UnfoldAdminSelectWidget,
        )
        self.fields["receipt_zone"].widget.attrs["data-shows-for"] = TransactionTypeChoices.RECEIPT
        self.fields["receipt_zone"].parameter_group = receipt_group

    def _add_porte_fields(self, company):
        planning = self.planning
        porte = data_schema_fields(TransactionTypeChoices.PORTE_MOVEMENT, company)
        group = _("Porte Details")
        plate_types = [
            TransactionTypeChoices.PORTE_MOVEMENT,
            TransactionTypeChoices.PORTE_DELIVERY,
        ]
        if planning.is_machine_porte:
            plate_types.append(TransactionTypeChoices.PORTE_LOAD)
        if not planning.transport_plate():
            self._add_plate_field(",".join(plate_types), group)
        for name in self._PORTE_LOAD_FIELDS:
            self._add_schema_field(name, porte[name], TransactionTypeChoices.PORTE_LOAD, group)

    def _shown(self, name, transaction_type):
        field = self.fields.get(name)
        if field is None:
            return False
        allowed = field.widget.attrs.get("data-shows-for", "").split(",")
        return transaction_type in allowed

    def clean(self):
        cleaned = super().clean()
        transaction_type = cleaned.get("transaction_type")
        plate = cleaned.get(PorteTransactionDataKey.MATRICULA)
        if plate is not None and not isinstance(plate, str):
            cleaned[PorteTransactionDataKey.MATRICULA] = plate.matricula or ""
        if self.planning is not None and "asset" in self.fields and not cleaned.get("asset"):
            machine_delivery = (
                transaction_type == TransactionTypeChoices.PORTE_DELIVERY
                and self.planning.machine_id
            )
            if not machine_delivery:
                self.add_error("asset", _("Pick the container. The planning doesn't have one yet."))
        if transaction_type in (TransactionTypeChoices.MOVEMENT, TransactionTypeChoices.DELIVERY):
            self._clean_container(cleaned, transaction_type)
        elif transaction_type == TransactionTypeChoices.RECEIPT:
            self._clean_receipt(cleaned)
        if transaction_type == TransactionTypeChoices.DELIVERY and "zone" in self.fields and not cleaned.get("zone"):
            self.add_error("zone", _("Choose a destination zone, or set one on the planning."))
        if self._shown(PorteTransactionDataKey.MATRICULA, transaction_type) and not (cleaned.get(PorteTransactionDataKey.MATRICULA) or "").strip():
            self.add_error(PorteTransactionDataKey.MATRICULA, _("Plate is required."))
        return cleaned

    def _clean_container(self, cleaned, transaction_type):
        full = cleaned.get(CubaTransactionKey.ESTADO) == CubaFillState.FULL
        if not cleaned.get(CubaTransactionKey.ESTADO):
            self.add_error(CubaTransactionKey.ESTADO, _("Container state is required."))
        if self._shown(CubaTransactionKey.M3, transaction_type) and not (cleaned.get(CubaTransactionKey.M3) or "").strip():
            self.add_error(CubaTransactionKey.M3, _("m³ is required."))
        if full:
            if self._shown(CubaTransactionKey.MATERIAL, transaction_type) and not (cleaned.get(CubaTransactionKey.MATERIAL) or "").strip():
                self.add_error(CubaTransactionKey.MATERIAL, _("Container material is required when the container is full."))
            if transaction_type == TransactionTypeChoices.MOVEMENT and not cleaned.get(CubaTransactionKey.VAS_A_VERTER):
                self.add_error(CubaTransactionKey.VAS_A_VERTER, _("Say whether it is going to be dumped."))
        else:
            cleaned[CubaTransactionKey.MATERIAL] = ""
        if transaction_type != TransactionTypeChoices.MOVEMENT:
            cleaned.pop(CubaTransactionKey.VAS_A_VERTER, None)
            cleaned.pop(CubaTransactionKey.LANDFILL, None)
            return
        dumping = full and cleaned.get(CubaTransactionKey.VAS_A_VERTER) == FormAffirmative.YES
        if not full:
            cleaned[CubaTransactionKey.VAS_A_VERTER] = ""
        if dumping and not cleaned.get(CubaTransactionKey.LANDFILL):
            self.add_error(CubaTransactionKey.LANDFILL, _("The landfill is required when it is going to be dumped."))
        if not dumping:
            cleaned[CubaTransactionKey.LANDFILL] = None

    def _clean_receipt(self, cleaned):
        if not (cleaned.get(CubaTransactionKey.ALBARAN) or "").strip():
            self.add_error(CubaTransactionKey.ALBARAN, _("An albaran number is required to register a receipt (verteo)."))
        if not cleaned.get("receipt_zone"):
            self.add_error("receipt_zone", _("The landfill is required."))
        else:
            cleaned["zone"] = cleaned["receipt_zone"]

    def build_data(self):
        """Typed fields for AssetTransaction.data. peso_neto is verteo-only and
        is not part of the shared receipt schema used by the transaction editor."""
        transaction_type = self.cleaned_data.get("transaction_type")
        data = build_data_from_cleaned(transaction_type, self.cleaned_data)
        if transaction_type == TransactionTypeChoices.RECEIPT:
            data[CubaTransactionKey.NET_WEIGHT] = (self.cleaned_data.get(CubaTransactionKey.NET_WEIGHT) or "").strip()
        return data


class SplitReceiptForm(forms.Form):
    """"Split into multiple DIs" / "Add more DI parts" wizard form, launched
    from an AssetTransactionReceipt's change page -- works whether this DI is
    still unsplit (1 of 1) or already has several parts. See
    assets.views.asset_transaction_wizard.ReceiptSplitWizardView."""

    def __init__(self, *args, current_total=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["total_parts"] = forms.IntegerField(
            label=_("How many DI parts should this load have in total?"),
            min_value=current_total + 1,
            max_value=MAX_RECEIPT_SPLIT_PARTS,
            initial=min(current_total + 1, MAX_RECEIPT_SPLIT_PARTS),
            widget=UnfoldAdminIntegerFieldWidget,
            help_text=_(
                "Existing parts keep their data; the new ones are created blank for you to "
                "fill in one residue type each."
            ),
        )

