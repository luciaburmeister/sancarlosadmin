import datetime

from django import forms
from django.contrib import admin
from django.contrib.admin.widgets import AutocompleteSelect
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.widgets import (
    UnfoldAdminDateWidget,
    UnfoldAdminSelect2MultipleWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminTimeWidget,
    UnfoldBooleanSwitchWidget,
)


class ResourcePlanningTimeWidget(UnfoldAdminTimeWidget):
    """Time picker with business-hour shortcuts (7:00–19:00) for planning forms."""

    class Media(UnfoldAdminTimeWidget.Media):
        js = UnfoldAdminTimeWidget.Media.js + [
            "management/js/resource_planning_time_shortcuts.js",
        ]


class InternalLabelsSelectWidget(UnfoldAdminSelect2MultipleWidget):
    """Select2 multi-select with static options (no Django admin AJAX)."""

    def __init__(self, attrs=None, choices=()):
        attrs = attrs or {}
        attrs["data-theme"] = "admin-autocomplete"
        attrs["class"] = "unfold-admin-autocomplete"
        # Call SelectMultiple directly — parent adds admin-autocomplete which
        # triggers djangoAdminSelect2 AJAX and breaks M2M label picking.
        forms.SelectMultiple.__init__(self, attrs, choices)

    class Media(UnfoldAdminSelect2MultipleWidget.Media):
        pass


from assets.models import AssetType
from .models import (
    ProjectFile,
    ProjectFileStatus,
    ResourcePlanning,
    ResourcePlanningType,
    ResourcePlanningStatus,
)

class LinkProjectFileForm(forms.Form):
    CHOICES = [
        ('existing', _('Link to an existing Expediente')),
        ('new', _('Create a new Expediente')),
    ]
    action_type = forms.ChoiceField(
        choices=CHOICES, 
        widget=forms.RadioSelect, 
        initial='new',
        label=_("How would you like to proceed?")
    )
    existing_project = forms.ModelChoiceField(
        queryset=ProjectFile.objects.exclude(status__in=[ProjectFileStatus.COMPLETED, ProjectFileStatus.CANCELLED]),
        required=False,
        label=_("Existing Expediente"),
        help_text=_("Only required if linking to an existing one.")
    )
    new_project_name = forms.CharField(
        max_length=255,
        required=False,
        label=_("New Expediente Name"),
        help_text=_("Only required if creating a new one. Defaults to the Quote's project name.")
    )

    def clean(self):
        cleaned_data = super().clean()
        action_type = cleaned_data.get("action_type")
        existing_project = cleaned_data.get("existing_project")
        new_project_name = cleaned_data.get("new_project_name")

        if action_type == 'existing' and not existing_project:
            self.add_error('existing_project', _("Please select an existing expediente."))
        
        return cleaned_data


# ponytail: one form, unfold widgets, minimal fields
def _parse_resource_tokens(raw: str) -> list[tuple[ContentType, int]]:
    """Parse 'employee:12,machine:5' into (ContentType, pk) pairs."""
    from django.apps import apps

    _model_map = {
        "employee": ("company", "Employee"),
        "machine": ("company", "Machine"),
        "asset": ("assets", "Asset"),
    }
    pairs = []
    for token in raw.split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        kind, pk_str = token.split(":", 1)
        if kind not in _model_map:
            continue
        try:
            pk = int(pk_str)
        except ValueError:
            continue
        app_label, model_name = _model_map[kind]
        Model = apps.get_model(app_label, model_name)
        ct = ContentType.objects.get_for_model(Model)
        pairs.append((ct, pk))
    return pairs


class ResourcePlanningFilterForm(forms.Form):
    company = forms.MultipleChoiceField(
        label=_("Company"),
        required=False,
        widget=forms.SelectMultiple,
    )
    day = forms.DateField(
        label=_("Start date"),
        required=True,
        initial=timezone.localdate,
        widget=UnfoldAdminDateWidget,
    )
    num_days = forms.IntegerField(
        label=_("Days"),
        required=True,
        initial=7,
        min_value=1,
        max_value=90,
        widget=UnfoldAdminIntegerFieldWidget,
    )
    planning_type = forms.ModelMultipleChoiceField(
        label=_("Planning Type"),
        required=False,
        queryset=ResourcePlanningType.objects.none(),
        widget=forms.SelectMultiple,
    )
    status = forms.MultipleChoiceField(
        label=_("Status"),
        required=False,
        choices=list(ResourcePlanningStatus.choices),
        widget=forms.SelectMultiple,
    )
    project_file = forms.ModelChoiceField(
        label=_("Project File"),
        required=False,
        queryset=ProjectFile.objects.none(),
        empty_label=_("All projects"),
    )
    asset_type = forms.ModelMultipleChoiceField(
        label=_("Asset Type"),
        required=False,
        queryset=AssetType.objects.none(),
        widget=forms.SelectMultiple,
    )
    only_with_plans = forms.BooleanField(
        label=_("Only resources with plannings"),
        required=False,
        initial=False,
        widget=UnfoldBooleanSwitchWidget,
    )
    resources = forms.CharField(
        label=_("Resources"),
        required=False,
        widget=forms.HiddenInput,
        help_text=_("Show only the selected employees, machines or assets."),
    )

    def __init__(self, *args, company=None, user_companies=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Company choices — built from the user's accessible companies
        companies = list(user_companies.order_by("name")) if user_companies else []
        company_choices = [("", _("All companies"))] + [(c.pk, c.name) for c in companies]
        self.fields["company"].choices = company_choices
        if company:
            self.fields["company"].initial = [str(company.pk)]
        if len(companies) <= 1:
            self.fields["company"].widget = forms.HiddenInput()

        pt_qs = ResourcePlanningType.objects.filter(is_active=True).order_by("name")
        pf_qs = ProjectFile.objects.exclude(
            status__in=[ProjectFileStatus.COMPLETED, ProjectFileStatus.CANCELLED]
        ).order_by("name")
        at_qs = AssetType.objects.order_by("name")
        if company:
            pt_qs = pt_qs.filter(company=company)
            pf_qs = pf_qs.filter(company=company)
            at_qs = at_qs.filter(company=company)
        elif user_companies is not None:
            pt_qs = pt_qs.filter(company__in=user_companies)
            pf_qs = pf_qs.filter(company__in=user_companies)
            at_qs = at_qs.filter(company__in=user_companies)
        self.fields["planning_type"].queryset = pt_qs
        self.fields["asset_type"].queryset = at_qs
        self.fields["project_file"].queryset = pf_qs
        # Autocomplete widget — uses ProjectFileAdmin.search_fields
        class _ProjectFileAutocomplete(AutocompleteSelect):
            """Fix data attrs so the autocomplete URL resolves correctly."""
            def build_attrs(self, base_attrs, extra_attrs=None):
                attrs = super().build_attrs(base_attrs, extra_attrs)
                attrs["data-app-label"] = "management"
                attrs["data-model-name"] = "resourceplanning"
                attrs["data-field-name"] = "project_file"
                return attrs

        rel = ResourcePlanning._meta.get_field("project_file").remote_field
        self.fields["project_file"].widget = _ProjectFileAutocomplete(rel, admin.site)
        self.fields["project_file"].widget.choices = self.fields["project_file"].choices

    def clean_resources(self) -> list[tuple[ContentType, int]]:
        raw = self.cleaned_data.get("resources", "")
        return _parse_resource_tokens(raw)

    class Media:
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]


# Apple-calendar-style repeat options
REPEAT_CHOICES = [
    ("never", _("Never")),
    ("daily", _("Every day")),
    ("weekly", _("Every week")),
    ("biweekly", _("Every 2 weeks")),
    ("monthly", _("Every month")),
    ("yearly", _("Every year")),
    ("custom", _("Custom (every N days)")),
]


def _clean_repeat_fields(form, cleaned):
    """Shared validation for the Apple-calendar-style repeat fields."""
    repeat = cleaned.get("repeat") or "never"
    cleaned["repeat"] = repeat
    if repeat != "never":
        repeat_until = cleaned.get("repeat_until")
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to and date_from != date_to:
            form.add_error(
                "repeat",
                _("Repetition is only available for same-day plannings."),
            )
        if not repeat_until:
            form.add_error("repeat_until", _("Choose an end date for the repetition."))
        elif date_from and repeat_until < date_from:
            form.add_error("repeat_until", _("Must be on or after the start date."))
        if repeat == "custom":
            every_days = cleaned.get("repeat_every_days")
            if every_days is None:
                form.add_error("repeat_every_days", _("Enter how many days between repetitions."))
            elif every_days < 1:
                form.add_error("repeat_every_days", _("Must be at least 1 day."))
            elif every_days > 365:
                form.add_error("repeat_every_days", _("Must be 365 days or fewer."))
    return cleaned


class ResourcePlanningBulkForm(forms.Form):
    """
    Form for creating or group-editing multiple ResourcePlanning records at once.
    The 'resources' field accepts a comma-separated token string built by the
    resource picker JS widget (e.g. "employee:12,machine:5").
    """
    company = forms.ModelChoiceField(
        queryset=ProjectFile.objects.none(),
        label=_("Company"),
        widget=UnfoldAdminSelectWidget,
    )
    project_file = forms.ModelChoiceField(
        queryset=ProjectFile.objects.none(),
        label=_("Project File"),
    )
    planning_type = forms.ModelChoiceField(
        queryset=ResourcePlanningType.objects.none(),
        label=_("Planning Type"),
        widget=UnfoldAdminSelectWidget,
    )
    status = forms.ChoiceField(
        choices=ResourcePlanningStatus.choices,
        initial=ResourcePlanningStatus.PLANNED,
        label=_("Status"),
        widget=UnfoldAdminSelectWidget,
    )
    notes = forms.CharField(
        required=False,
        label=_("Notes"),
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 3}),
    )
    resources = forms.CharField(
        label=_("Resources"),
        help_text=_("Use the picker to select employees, machines or assets."),
        widget=forms.HiddenInput,
    )
    date_from = forms.DateField(
        label=_("From"),
        widget=UnfoldAdminDateWidget,
    )
    date_to = forms.DateField(
        label=_("To"),
        widget=UnfoldAdminDateWidget,
    )
    daily_start = forms.TimeField(
        label=_("Daily start"),
        initial=datetime.time(8, 0),
        widget=ResourcePlanningTimeWidget,
    )
    daily_end = forms.TimeField(
        label=_("Daily end"),
        initial=datetime.time(17, 0),
        widget=ResourcePlanningTimeWidget,
    )
    exclude_weekends = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Exclude weekends"),
        widget=UnfoldBooleanSwitchWidget,
    )
    repeat = forms.ChoiceField(
        choices=REPEAT_CHOICES,
        initial="never",
        required=False,
        label=_("Repeat"),
        widget=UnfoldAdminSelectWidget,
    )
    repeat_until = forms.DateField(
        required=False,
        label=_("End repeat"),
        widget=UnfoldAdminDateWidget,
    )
    repeat_every_days = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=365,
        label=_("Every N days"),
        widget=UnfoldAdminIntegerFieldWidget(attrs={"placeholder": "2"}),
    )
    is_group = forms.BooleanField(
        required=False,
        initial=False,
        label=_("Edit as a group"),
        help_text=_("Group these plannings so they can be updated or cancelled together."),
        widget=UnfoldBooleanSwitchWidget,
    )
    show_planning_type = forms.BooleanField(
        required=False,
        initial=False,
        label=_("Show planning type"),
        help_text=_("Label these plannings with the planning type instead of the project file name in the schedule view."),
        widget=UnfoldBooleanSwitchWidget,
    )
    shift_following = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.HiddenInput,
        help_text=_("Push the resource's later plannings of the same day forward by this planning's duration."),
    )
    backlog_item = forms.IntegerField(
        required=False,
        widget=forms.HiddenInput,
        help_text=_("Backlog item that will be marked as scheduled when these plannings are created."),
    )

    def __init__(self, *args, user=None, user_companies=None, **kwargs):
        super().__init__(*args, **kwargs)
        from company.models import Company

        if user_companies is not None:
            self.fields["company"].queryset = user_companies.order_by("name")
            self.fields["project_file"].queryset = ProjectFile.objects.filter(
                company__in=user_companies
            ).exclude(
                status__in=[ProjectFileStatus.COMPLETED, ProjectFileStatus.CANCELLED]
            ).order_by("name")
            self.fields["planning_type"].queryset = ResourcePlanningType.objects.filter(
                company__in=user_companies, is_active=True
            ).order_by("name")

    def clean_resources(self) -> list[tuple[ContentType, int]]:
        raw = self.cleaned_data.get("resources", "")
        pairs = _parse_resource_tokens(raw)
        if not pairs:
            raise forms.ValidationError(_("Select at least one resource."))
        return pairs

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        daily_start = cleaned.get("daily_start")
        daily_end = cleaned.get("daily_end")
        if not all([date_from, date_to, daily_start, daily_end]):
            return cleaned
        if date_to < date_from:
            self.add_error("date_to", _("End date must be on or after start date."))
            return cleaned
        start_dt = datetime.datetime.combine(date_from, daily_start)
        end_dt = datetime.datetime.combine(date_to, daily_end)
        if end_dt <= start_dt:
            if date_from == date_to:
                self.add_error(
                    "daily_end",
                    _("Daily end time must be after daily start time."),
                )
            else:
                self.add_error(
                    "daily_end",
                    _("End date/time must be after start date/time."),
                )
        return _clean_repeat_fields(self, cleaned)

    @property
    def resource_pairs(self) -> list[tuple[ContentType, int]]:
        return self.cleaned_data.get("resources", [])


class ResourcePlanningMultiForm(forms.Form):
    """
    Shared fields for creating several independent plannings at once.
    Company, status and resources are common to every plan line; everything
    else (project file, type, dates, hours, options) lives in
    ResourcePlanningMultiLineForm.
    """
    company = forms.ModelChoiceField(
        queryset=ProjectFile.objects.none(),
        label=_("Company"),
        widget=UnfoldAdminSelectWidget,
    )
    status = forms.ChoiceField(
        choices=ResourcePlanningStatus.choices,
        initial=ResourcePlanningStatus.PLANNED,
        label=_("Status"),
        widget=UnfoldAdminSelectWidget,
    )
    resources = forms.CharField(
        label=_("Resources"),
        help_text=_("Use the picker to select employees, machines or assets."),
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, user=None, user_companies=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user_companies is not None:
            self.fields["company"].queryset = user_companies.order_by("name")

    def clean_resources(self) -> list[tuple[ContentType, int]]:
        raw = self.cleaned_data.get("resources", "")
        pairs = _parse_resource_tokens(raw)
        if not pairs:
            raise forms.ValidationError(_("Select at least one resource."))
        return pairs

    @property
    def resource_pairs(self) -> list[tuple[ContentType, int]]:
        return self.cleaned_data.get("resources", [])


class ResourcePlanningMultiLineForm(forms.Form):
    """One plan line: its own project file, planning type, dates, hours and options."""
    project_file = forms.ModelChoiceField(
        queryset=ProjectFile.objects.none(),
        label=_("Project File"),
        widget=UnfoldAdminSelectWidget,
    )
    planning_type = forms.ModelChoiceField(
        queryset=ResourcePlanningType.objects.none(),
        label=_("Planning Type"),
        widget=UnfoldAdminSelectWidget,
    )
    date_from = forms.DateField(
        label=_("From"),
        widget=UnfoldAdminDateWidget,
    )
    date_to = forms.DateField(
        label=_("To"),
        widget=UnfoldAdminDateWidget,
    )
    daily_start = forms.TimeField(
        label=_("Daily start"),
        initial=datetime.time(8, 0),
        widget=ResourcePlanningTimeWidget,
    )
    daily_end = forms.TimeField(
        label=_("Daily end"),
        initial=datetime.time(17, 0),
        widget=ResourcePlanningTimeWidget,
    )
    exclude_weekends = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Exclude weekends"),
        widget=UnfoldBooleanSwitchWidget,
    )
    show_planning_type = forms.BooleanField(
        required=False,
        initial=False,
        label=_("Show planning type"),
        widget=UnfoldBooleanSwitchWidget,
    )
    notes = forms.CharField(
        required=False,
        label=_("Notes"),
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 2}),
    )
    repeat = forms.ChoiceField(
        choices=REPEAT_CHOICES,
        initial="never",
        required=False,
        label=_("Repeat"),
        widget=UnfoldAdminSelectWidget,
    )
    repeat_until = forms.DateField(
        required=False,
        label=_("End repeat"),
        widget=UnfoldAdminDateWidget,
    )
    repeat_every_days = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=365,
        label=_("Every N days"),
        widget=UnfoldAdminIntegerFieldWidget(attrs={"placeholder": "2"}),
    )

    def __init__(self, *args, user_companies=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user_companies is not None:
            self.fields["project_file"].queryset = ProjectFile.objects.filter(
                company__in=user_companies
            ).exclude(
                status__in=[ProjectFileStatus.COMPLETED, ProjectFileStatus.CANCELLED]
            ).order_by("name")
            self.fields["planning_type"].queryset = ResourcePlanningType.objects.filter(
                company__in=user_companies, is_active=True
            ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        daily_start = cleaned.get("daily_start")
        daily_end = cleaned.get("daily_end")
        if not all([date_from, date_to, daily_start, daily_end]):
            return cleaned
        if date_to < date_from:
            self.add_error("date_to", _("End date must be on or after start date."))
            return cleaned
        start_dt = datetime.datetime.combine(date_from, daily_start)
        end_dt = datetime.datetime.combine(date_to, daily_end)
        if end_dt <= start_dt:
            if date_from == date_to:
                self.add_error(
                    "daily_end",
                    _("Daily end time must be after daily start time."),
                )
            else:
                self.add_error(
                    "daily_end",
                    _("End date/time must be after start date/time."),
                )
        return _clean_repeat_fields(self, cleaned)


ResourcePlanningMultiLineFormSet = forms.formset_factory(
    ResourcePlanningMultiLineForm,
    extra=0,
    min_num=1,
    validate_min=True,
    can_delete=True,
)


class ResourcePlanningSeriesForm(forms.Form):
    """
    Editor for a repetition series: changes apply to every occurrence in the
    group without touching their dates. Daily hours are re-applied on each
    occurrence's own day.
    """
    project_file = forms.ModelChoiceField(
        queryset=ProjectFile.objects.none(),
        label=_("Project File"),
    )
    planning_type = forms.ModelChoiceField(
        queryset=ResourcePlanningType.objects.none(),
        label=_("Planning Type"),
        widget=UnfoldAdminSelectWidget,
    )
    status = forms.ChoiceField(
        choices=ResourcePlanningStatus.choices,
        label=_("Status"),
        widget=UnfoldAdminSelectWidget,
    )
    daily_start = forms.TimeField(
        label=_("Daily start"),
        widget=ResourcePlanningTimeWidget,
    )
    daily_end = forms.TimeField(
        label=_("Daily end"),
        widget=ResourcePlanningTimeWidget,
    )
    show_planning_type = forms.BooleanField(
        required=False,
        label=_("Show planning type"),
        widget=UnfoldBooleanSwitchWidget,
    )
    notes = forms.CharField(
        required=False,
        label=_("Notes"),
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 3}),
    )

    def __init__(self, *args, user_companies=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user_companies is not None:
            self.fields["project_file"].queryset = ProjectFile.objects.filter(
                company__in=user_companies
            ).exclude(
                status__in=[ProjectFileStatus.COMPLETED, ProjectFileStatus.CANCELLED]
            ).order_by("name")
            self.fields["planning_type"].queryset = ResourcePlanningType.objects.filter(
                company__in=user_companies, is_active=True
            ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        daily_start = cleaned.get("daily_start")
        daily_end = cleaned.get("daily_end")
        if daily_start and daily_end and daily_end <= daily_start:
            self.add_error("daily_end", _("Daily end time must be after daily start time."))
        return cleaned


