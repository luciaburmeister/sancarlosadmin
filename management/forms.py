from django import forms
from django.utils.translation import gettext_lazy as _
from .models import ProjectFile, ProjectFileStatus

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
