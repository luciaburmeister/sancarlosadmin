import datetime

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

from company.models import Company, Employee
from core.admin.company_access import get_user_companies
from core.forms.dynamic_fields import DynamicFieldBuilder
from sales.models import Client

from .models import (
    Asset,
    AssetIdentifier,
    AssetPlanning,
    AssetType,
    AssetTypeCategory,
    PlanningUrgency,
    ServiceType,
)

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


