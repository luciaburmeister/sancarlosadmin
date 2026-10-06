import datetime
from datetime import timedelta

from django import forms
from django.utils.translation import gettext_lazy as _
from unfold.fields import UnfoldAdminAutocompleteModelChoiceField
from unfold.widgets import (
    UnfoldAdminTextInputWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminSelect2MultipleWidget,
    UnfoldBooleanSwitchWidget,
)


class StaticSelect2MultipleWidget(UnfoldAdminSelect2MultipleWidget):
    """Select2 multi-select with local options (chips + search), like schedule filters.

    UnfoldAdminSelect2MultipleWidget adds ``admin-autocomplete``, which triggers
    Django's AJAX autocomplete and breaks static M2M picks in ModelAdmin forms.
    """

    def __init__(self, attrs=None, choices=()):
        attrs = attrs or {}
        attrs["data-theme"] = "admin-autocomplete"
        attrs["class"] = "unfold-admin-autocomplete"
        forms.SelectMultiple.__init__(self, attrs, choices)

    class Media(UnfoldAdminSelect2MultipleWidget.Media):
        pass


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
    AssetTypeCategory,
    PlanningType,
    PlanningUrgency,
    ServiceType,
    TransactionTypeChoices,
)
from .transaction_data_schema import build_data_from_cleaned, data_schema_fields, match_choice_value

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
    """"Create Transaction" wizard form, launched from an AssetPlanning's
    change page. Pre-filled by the view from the planning; see
    assets.views.asset_transaction_wizard.AssetTransactionFromPlanningWizardView.
    """

    transaction_type = forms.ChoiceField(
        label=_("Type"),
        choices=CUBA_TRANSACTION_TYPE_CHOICES,
        widget=UnfoldAdminSelectWidget,
    )
    asset = forms.ModelChoiceField(
        label=_("Asset"),
        queryset=Asset.objects.none(),
        required=False,
        widget=UnfoldAdminSelectWidget,
        help_text=_("Only needed if the planning has no asset/truck/machine assigned yet — picks what this transaction is recorded against. Pre-filled from the planning when it already has one."),
    )
    zone = forms.ModelChoiceField(
        label=_("Zone"),
        queryset=WorkingZone.objects.none(),
        required=False,
        help_text=_("Delivery: destination zone (defaults to the planning's destination if left blank). Receipt: the landfill/vertedero, if known. Not used for Movement or Porte (porte always uses the planning's own origin/destination)."),
    )
    performed_by = forms.ModelChoiceField(
        label=_("Performed by"),
        queryset=Employee.objects.none(),
        required=False,
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": _("e.g. Manual correction: driver's phone failed.")}),
    )

    def __init__(self, *args, planning=None, **kwargs):
        self.planning = planning
        super().__init__(*args, **kwargs)
        company = planning.company if planning is not None else None
        # "" (falsy) renders these ungrouped, above the per-type sections below.
        for name in ("transaction_type", "asset", "zone", "performed_by", "notes"):
            self.fields[name].parameter_group = ""
        # Porte always uses the planning's own origin/destination, never `zone`.
        self.fields["zone"].widget.attrs["data-shows-for"] = "movement,delivery,receipt"
        if planning is not None and planning.planning_type == PlanningType.PORTE:
            self.fields["transaction_type"].choices = PORTE_TRANSACTION_TYPE_CHOICES
        if planning is not None and planning.company_id:
            self.fields["asset"].queryset = Asset.objects.filter(
                company_id=planning.company_id
            ).order_by("name")
            # Planning may already have one on any of these, depending on type/step
            # (cuba: asset; porte: transport_asset or, for an own-machine porte,
            # machine) -- pre-fill from whichever is actually set so re-submitting
            # a correction doesn't require re-picking it.
            current_asset = planning.asset or planning.transport_asset or planning.machine
            if current_asset is not None:
                self.fields["asset"].initial = current_asset.pk
            self.fields["zone"].queryset = WorkingZone.objects.filter(
                company_id=planning.company_id
            ).order_by("name")
            self.fields["performed_by"].queryset = Employee.objects.filter(
                company_id=planning.company_id, is_active=True
            ).order_by("first_name", "last_name")
            self.fields["zone"].initial = planning.destination_id
            self.fields["performed_by"].initial = planning.assigned_to_id
        # Only the families relevant to this planning's type are built at all.
        # A field name shared across families (e.g. "matricula") must NOT be
        # replaced by a later loop -- that would drop the earlier family's
        # data-shows-for value and hide the field for it entirely.
        if planning is not None and planning.planning_type == PlanningType.PORTE:
            families = [
                (_("Porte Details"), "porte_movement,porte_load,porte_delivery", TransactionTypeChoices.PORTE_MOVEMENT),
            ]
        else:
            families = [
                (_("Container Details"), "movement,delivery", TransactionTypeChoices.MOVEMENT),
                (_("Receipt (Verteo) Details"), "receipt", TransactionTypeChoices.RECEIPT),
            ]
        for parameter_group, shows_for, field_transaction_type in families:
            for name, field in data_schema_fields(field_transaction_type, company).items():
                if name in self.fields:
                    existing = self.fields[name].widget.attrs.get("data-shows-for", "")
                    field.widget.attrs["data-shows-for"] = ",".join(
                        filter(None, [existing, shows_for])
                    )
                else:
                    field.widget.attrs["data-shows-for"] = shows_for
                field.parameter_group = self.fields[name].parameter_group if name in self.fields else parameter_group
                self.fields[name] = field

        # The planning already carries this info under its own name/format
        # (cuba_size is e.g. "7m3") -- pre-fill rather than retype it.
        if planning is not None and planning.cuba_size and "m3" in self.fields:
            self.fields["m3"].initial = match_choice_value(
                self.fields["m3"], AssetPlanning.normalize_cuba_size(planning.cuba_size)
            )
        if planning is not None and planning.load_description and "tipo_cuba" in self.fields:
            self.fields["tipo_cuba"].initial = match_choice_value(
                self.fields["tipo_cuba"], planning.load_description.strip()
            )
        # Pre-filled from the planning but left editable, in case it's wrong.
        if planning is not None and planning.load_description and "load_description" in self.fields:
            self.fields["load_description"].initial = planning.load_description.strip()

    def clean(self):
        cleaned = super().clean()
        transaction_type = cleaned.get("transaction_type")
        if self.planning is not None and not cleaned.get("asset"):
            planning_has_asset = bool(
                self.planning.asset_id or self.planning.transport_asset_id or self.planning.machine_id
            )
            if not planning_has_asset:
                self.add_error("asset", _("This planning has no asset/truck/machine assigned yet — pick one here to continue."))
        if transaction_type == TransactionTypeChoices.DELIVERY:
            zone = cleaned.get("zone")
            destination = self.planning.destination if self.planning else None
            if not zone and not destination:
                self.add_error("zone", _("Choose a destination zone, or set one on the planning."))
        elif transaction_type == TransactionTypeChoices.RECEIPT:
            if not (cleaned.get("nro_albaran") or "").strip():
                self.add_error("nro_albaran", _("An albaran number is required to register a receipt (verteo)."))
        return cleaned

    def build_data(self):
        """The typed container fields, assembled into an AssetTransaction.data
        dict via the shared schema (see assets.transaction_data_schema)."""
        return build_data_from_cleaned(self.cleaned_data.get("transaction_type"), self.cleaned_data)

