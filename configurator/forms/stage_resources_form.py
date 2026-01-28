from django import forms
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Div, Field
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminIntegerFieldWidget

from company.models import EmployeeType, MachineType


class StagePlanningForm(forms.Form):
    """
    One form per production stage. Indicate resources and time.
    """

    stage_key = forms.CharField(widget=forms.HiddenInput(), required=True)

    employee_type = forms.ModelChoiceField(
        queryset=EmployeeType.objects.all(),
        required=False,
        label=_("Employee type"),
        widget=UnfoldAdminSelectWidget,
    )
    employee_qty = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        label=_("Quantity"),
        widget=UnfoldAdminIntegerFieldWidget,
    )

    machine_type = forms.ModelChoiceField(
        queryset=MachineType.objects.all(),
        required=False,
        label=_("Machine type"),
        widget=UnfoldAdminSelectWidget,
    )
    machine_qty = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        label=_("Quantity"),
        widget=UnfoldAdminIntegerFieldWidget,
    )

    prep_time_min = forms.IntegerField(
        min_value=0,
        required=True,
        initial=0,
        label=_("Prep time"),
        widget=UnfoldAdminIntegerFieldWidget,
    )
    work_time_min = forms.IntegerField(
        min_value=0,
        required=True,
        initial=0,
        label=_("Work time"),
        widget=UnfoldAdminIntegerFieldWidget,
    )
    post_time_min = forms.IntegerField(
        min_value=0,
        required=True,
        initial=0,
        label=_("Post time"),
        widget=UnfoldAdminIntegerFieldWidget,
    )

    def __init__(self, *args, company=None, request=None, admin_site=None, **kwargs):
        super().__init__(*args, **kwargs)

        # 1) Scope dropdown choices to company
        if company is not None:
            self.fields["employee_type"].queryset = EmployeeType.objects.filter(
                company=company
            ).order_by("type")
            self.fields["machine_type"].queryset = MachineType.objects.filter(
                company=company
            ).order_by("type")

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True

        self.helper.layout = Layout(
            "stage_key", 
            # --- Employee Section ---
            Div(
                Field("employee_type", wrapper_class="w-full lg:w-3/4"),
                Field("employee_qty", wrapper_class="w-full lg:w-1/4"),
                css_class="flex flex-col lg:flex-row gap-4 w-full mb-4"
            ),

            # --- Machine Section ---
            Div(
                Field("machine_type", wrapper_class="w-full lg:w-3/4"),
                Field("machine_qty", wrapper_class="w-full lg:w-1/4"),
                css_class="flex flex-col lg:flex-row gap-4 w-full mb-4"
            ),
            
            # --- Time Section ---
            Div(
                Field("prep_time_min", wrapper_class="w-full lg:w-1/3"),
                Field("work_time_min", wrapper_class="w-full lg:w-1/3"),
                Field("post_time_min", wrapper_class="w-full lg:w-1/3"),
                css_class="flex flex-col lg:flex-row gap-4 w-full"
            ),
        )

    def clean(self):
        """
        Consistency rule:
        - If type is chosen -> quantity must be > 0
        - If qty > 0 -> type must be chosen
        """
        cleaned = super().clean()

        et = cleaned.get("employee_type")
        eq = cleaned.get("employee_qty") or 0
        mt = cleaned.get("machine_type")
        mq = cleaned.get("machine_qty") or 0

        if et and eq <= 0:
            self.add_error(
                "employee_qty",
                _("Quantity must be > 0 when an employee type is selected."),
            )
        if eq > 0 and not et:
            self.add_error(
                "employee_type", _("Select an employee type when quantity is > 0.")
            )

        if mt and mq <= 0:
            self.add_error(
                "machine_qty",
                _("Quantity must be > 0 when a machine type is selected."),
            )
        if mq > 0 and not mt:
            self.add_error(
                "machine_type", _("Select a machine type when quantity is > 0.")
            )

        return cleaned


class BaseStagePlanningFormSet(forms.BaseFormSet):
    """
    Passes company + admin context (request/admin_site) into each form so:
    - dropdowns are company-scoped
    """

    def __init__(self, *args, company=None, request=None, admin_site=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.request = request
        self.admin_site = admin_site

    def _construct_form(self, i, **kwargs):
        kwargs["company"] = self.company
        kwargs["request"] = self.request
        kwargs["admin_site"] = self.admin_site
        return super()._construct_form(i, **kwargs)


StagePlanningFormSet = forms.formset_factory(
    StagePlanningForm,
    formset=BaseStagePlanningFormSet,
    extra=0,
)
