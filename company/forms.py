from django import forms
from .models import EmployeeDocument, MachineDocument, CompanyDocument

class BaseDocumentForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and getattr(self.instance, "does_not_expire", False):
            if "expiry_date" in self.fields:
                self.fields["expiry_date"].widget = forms.HiddenInput()
                self.fields["expiry_date"].label = ""

class EmployeeDocumentForm(BaseDocumentForm):
    class Meta:
        model = EmployeeDocument
        fields = "__all__"

class MachineDocumentForm(BaseDocumentForm):
    class Meta:
        model = MachineDocument
        fields = "__all__"

class CompanyDocumentForm(BaseDocumentForm):
    class Meta:
        model = CompanyDocument
        fields = "__all__"
