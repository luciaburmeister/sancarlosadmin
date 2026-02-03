from django import forms
from django.forms import BaseFormSet
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column, Div
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminIntegerFieldWidget

from company.models import EmployeeType, MachineType

class ResourcesFormsetHelper(FormHelper):
    """
    Crispy helper for the EmployeeForm and MachineForm formset, matching Unfold docs.
    Enables dynamic add/remove rows WITHOUT custom JS.
    """

    def __init__(self, form_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.template = "unfold_crispy/layout/table_inline_formset.html"
        self.form_id = form_id
        self.form_add = True                
        self.form_tag = False           # Wizard handles <form>
        self.template_pack = "unfold_crispy"

class EmployeeForm(forms.Form):

    employee_type = forms.ModelChoiceField(
        queryset=EmployeeType.objects.all(),
        required=False,
        label=_("Employee type"),
        widget=UnfoldAdminSelectWidget(),
    )
    employee_qty = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        label=_("Quantity"),
        widget=UnfoldAdminIntegerFieldWidget(),
    )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)

        if company is not None:
            self.fields["employee_type"].queryset = EmployeeType.objects.filter(
                company=company
            ).order_by("type")

        self.helper = FormHelper()
        self.helper.form_tag = False 
        self.helper.layout = Layout(
            Row(
                Column(Field("employee_type"), css_class="col-8"),
                Column(Field("employee_qty"), css_class="col-4"),
            )
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

        if et and eq <= 0:
            self.add_error(
                "employee_qty",
                _("Quantity must be > 0 when an employee type is selected."),
            )
        if eq > 0 and not et:
            self.add_error(
                "employee_type", _("Select an employee type when quantity is > 0.")
            )
        
        return cleaned

class BaseEmployeeFormSet(BaseFormSet):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["company"] = self.company
        return kwargs
    
EmployeeFormSet = forms.formset_factory(
    EmployeeForm,
    formset=BaseEmployeeFormSet,
    extra=0,
    min_num=1,
    can_delete=True,
)

class MachineForm(forms.Form):

    machine_type = forms.ModelChoiceField(
        queryset=MachineType.objects.all(),
        required=False,
        label=_("Machine type"),
        widget=UnfoldAdminSelectWidget(),
    )
    machine_qty = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        label=_("Quantity"),
        widget=UnfoldAdminIntegerFieldWidget(),
    )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        
        if company is not None:
            self.fields["machine_type"].queryset = MachineType.objects.filter(
                company=company
            ).order_by("type")

        self.helper = FormHelper()
        self.helper.form_tag = False 
        self.helper.layout = Layout(
            Row(
                Column(Field("machine_type"), css_class="col-8"),
                Column(Field("machine_qty"), css_class="col-4"),
            )
        )

    def clean(self):
        """
        Consistency rule:
        - If type is chosen -> quantity must be > 0
        - If qty > 0 -> type must be chosen
        """
        cleaned = super().clean()

        mt = cleaned.get("machine_type")
        mq = cleaned.get("machine_qty") or 0

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

class BaseMachineFormSet(BaseFormSet):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["company"] = self.company
        return kwargs

MachineFormSet = forms.formset_factory(
    MachineForm,
    formset=BaseMachineFormSet,
    extra=0,
    min_num=1,
    can_delete=True,
)

class StagePlanningForm(forms.Form):
    """
    One form per production stage. Indicate resources and time.
    """

    stage_key = forms.CharField(widget=forms.HiddenInput(), required=True)

    prep_time_min = forms.IntegerField(
        min_value=0,
        required=True,
        initial=0,
        label=_("Prep time"),
        widget=UnfoldAdminIntegerFieldWidget(),
    )
    work_time_min = forms.IntegerField(
        min_value=0,
        required=True,
        initial=0,
        label=_("Work time"),
        widget=UnfoldAdminIntegerFieldWidget(),
    )
    post_time_min = forms.IntegerField(
        min_value=0,
        required=True,
        initial=0,
        label=_("Post time"),
        widget=UnfoldAdminIntegerFieldWidget(),
    )

    def __init__(self, *args, company=None, request=None, admin_site=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True

        self.helper.layout = Layout(
            "stage_key", 
            Div(
                Field("prep_time_min", wrapper_class="w-full lg:w-1/3"),
                Field("work_time_min", wrapper_class="w-full lg:w-1/3"),
                Field("post_time_min", wrapper_class="w-full lg:w-1/3"),
                css_class="flex flex-col lg:flex-row gap-4 w-full"
            ),
        )

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
