from django import forms
from management.models import ProjectFile
from unfold.fields import UnfoldAdminAutocompleteModelChoiceField
from django.utils.translation import gettext_lazy as _

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column, HTML
from core.admin.company_access import get_user_companies

class ProjectFileSelectionFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"

        self.layout = Layout(
            HTML(
                f"""
                <div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                    <h3 class="text-base font-semibold text-gray-800 mb-3">
                        {_("Project Selection")}
                    </h3>
                """
            ),
            Row(
                Column(Field("project_file"), css_class="col-span-12"),
                css_class="gap-4 mb-4"
            ),
            HTML("</div>")
        )

class ProjectFileSelectionForm(forms.Form):
    project_file = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:project_autocomplete", # Wait, I need to check if this autocomplete exists
        queryset=ProjectFile.objects.all(),
        label=_("Select Project")
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.product = kwargs.pop("product", None)
        
        super().__init__(*args, **kwargs)
        
        if not self.is_bound:
            self.fields["project_file"].queryset = ProjectFile.objects.none()

        self.helper = ProjectFileSelectionFormHelper()
        
        if self.request and self.request.user and not self.request.user.is_superuser:
            user_companies = get_user_companies(self.request.user)
            self.fields["project_file"].queryset = self.fields["project_file"].queryset.filter(company__in=user_companies)
            
        if self.product and self.product.company:
            self.fields["project_file"].queryset = self.fields["project_file"].queryset.filter(company=self.product.company)
            
        # Add the related field wrapper if needed, though ProjectFile is not a field of Configuration anymore
        # But we might still want the 'plus' button to add a new project.
        # Since it's a forms.Form and PROJECT_FILE is not a model field of Configuration, 
        # we might need to manually set up the wrapper or just leave it.
