from django import forms
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminIntegerFieldWidget, UnfoldAdminTextInputWidget
from django.core.exceptions import ValidationError

from configurator.utils.evaluator import validate_expression_for_safe_eval


class StagePlanningForm(forms.Form):
    stage_key = forms.CharField(widget=forms.HiddenInput(), required=True)

    employee_qty = forms.IntegerField(
        min_value=0,
        required=True,
        initial=1,
        label=_("Employees required"),
        widget=UnfoldAdminIntegerFieldWidget(),
    )
    machine_qty = forms.IntegerField(
        min_value=0,
        required=True,
        initial=1,
        label=_("Machines required"),
        widget=UnfoldAdminIntegerFieldWidget(),
    )

    prep_time_min = forms.CharField(
        required=True,
        initial="0",
        label=_("Prep time"),
        widget=UnfoldAdminTextInputWidget(),
    )
    work_time_min = forms.CharField(
        required=True,
        initial="0",
        label=_("Work time"),
        widget=UnfoldAdminTextInputWidget(),
    )
    post_time_min = forms.CharField(
        required=True,
        initial="0",
        label=_("Post time"),
        widget=UnfoldAdminTextInputWidget(),
    )

    def clean(self):
        cleaned = super().clean()

        for field_name in (
            "prep_time_min",
            "work_time_min",
            "post_time_min",
        ):
            raw = (cleaned.get(field_name) or "").strip()

            if not raw:
                raise ValidationError({field_name: _("This field is required.")})

            try:
                validate_expression_for_safe_eval(raw)
            except ValueError as e:
                raise ValidationError(
                    {field_name: _("Invalid expression: %(error)s") % {"error": e}}
                )

            cleaned[field_name] = raw

        return cleaned


class BaseStagePlanningFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()

        seen = set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if form.cleaned_data.get("DELETE"):
                continue

            stage_key = form.cleaned_data.get("stage_key")
            if not stage_key:
                continue

            if stage_key in seen:
                raise ValidationError(_("Stage keys must be unique."))
            seen.add(stage_key)


StagePlanningFormSet = forms.formset_factory(
    StagePlanningForm,
    formset=BaseStagePlanningFormSet,
    extra=0,
)