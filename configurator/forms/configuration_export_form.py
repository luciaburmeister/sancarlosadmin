from django import forms
from unfold.widgets import UnfoldAdminSelect2Widget
class ConfigurationExportForm(forms.Form):
    template = forms.ChoiceField(label="Export Format", required=True, widget=UnfoldAdminSelect2Widget)

    def __init__(self, *args, available_templates=None, **kwargs):
        super().__init__(*args, **kwargs)

        if available_templates is None:
            available_templates = []

        self.fields["template"].choices = [
            (tpl.id, tpl.name) for tpl in available_templates
        ]
