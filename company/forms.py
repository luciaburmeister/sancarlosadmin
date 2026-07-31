from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminSplitDateTimeWidget

from .models import EmployeeDocument, MachineDocument, CompanyDocument, Employee

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


class DriverTrackingFilterForm(forms.Form):
    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        label=_("Driver"),
        empty_label=_("All"),
        widget=UnfoldAdminSelectWidget,
    )
    start = forms.SplitDateTimeField(
        required=False,
        label=_("From"),
        widget=UnfoldAdminSplitDateTimeWidget,
    )
    end = forms.SplitDateTimeField(
        required=False,
        label=_("To"),
        widget=UnfoldAdminSplitDateTimeWidget,
    )

    def __init__(self, *args, drivers=None, **kwargs):
        super().__init__(*args, **kwargs)
        if drivers is not None:
            self.fields["employee"].queryset = drivers

    class Media:
        extra = "" if settings.DEBUG else ".min"
        js = [
            f"admin/js/vendor/jquery/jquery{extra}.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]
