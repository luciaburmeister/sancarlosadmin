from django import forms
from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminDateWidget, UnfoldAdminSelectWidget, UnfoldAdminIntegerFieldWidget

from assets.models import AssetType
from .models import ProjectFile, ProjectFileStatus, ResourcePlanning, ResourcePlanningType, ResourcePlanningStatus

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
class ResourcePlanningFilterForm(forms.Form):
    company = forms.ChoiceField(
        label=_("Company"),
        required=False,
        widget=UnfoldAdminSelectWidget,
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
    planning_type = forms.ModelChoiceField(
        label=_("Planning Type"),
        required=False,
        queryset=ResourcePlanningType.objects.none(),
        empty_label=_("All types"),
        widget=UnfoldAdminSelectWidget,
    )
    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[("", _("All statuses"))] + list(ResourcePlanningStatus.choices),
        widget=UnfoldAdminSelectWidget,
    )
    project_file = forms.ModelChoiceField(
        label=_("Project File"),
        required=False,
        queryset=ProjectFile.objects.none(),
        empty_label=_("All projects"),
    )
    asset_type = forms.ModelChoiceField(
        label=_("Asset Type"),
        required=False,
        queryset=AssetType.objects.none(),
        empty_label=_("All asset types"),
        widget=UnfoldAdminSelectWidget,
    )

    def __init__(self, *args, company=None, user_companies=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Company choices — built from the user's accessible companies
        companies = list(user_companies.order_by("name")) if user_companies else []
        company_choices = [("", _("All companies"))] + [(c.pk, c.name) for c in companies]
        self.fields["company"].choices = company_choices
        if company:
            self.fields["company"].initial = str(company.pk)
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
        from django.contrib.admin.widgets import AutocompleteSelect

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

    class Media:
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]


