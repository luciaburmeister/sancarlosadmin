"""Step forms render with the admin's look without naming a widget per field."""
from django import forms
from unfold import widgets as unfold

UNFOLD_WIDGETS = {
    forms.TextInput: unfold.UnfoldAdminTextInputWidget,
    forms.EmailInput: unfold.UnfoldAdminEmailInputWidget,
    forms.NumberInput: unfold.UnfoldAdminDecimalFieldWidget,
    forms.Textarea: unfold.UnfoldAdminTextareaWidget,
    forms.Select: unfold.UnfoldAdminSelectWidget,
    forms.DateInput: unfold.UnfoldAdminSingleDateWidget,
    forms.TimeInput: unfold.UnfoldAdminSingleTimeWidget,
    forms.CheckboxInput: unfold.UnfoldBooleanSwitchWidget,
    forms.ClearableFileInput: unfold.UnfoldAdminFileFieldWidget,
    forms.FileInput: unfold.UnfoldAdminFileFieldWidget,
    forms.CheckboxSelectMultiple: unfold.UnfoldAdminCheckboxSelectMultipleWidget,
}


class UnfoldFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            unfold_widget = UNFOLD_WIDGETS.get(type(field.widget))
            if unfold_widget is None:
                continue
            widget = unfold_widget(attrs=field.widget.attrs)
            widget.is_required = field.widget.is_required
            if hasattr(field, "choices"):
                widget.choices = field.choices
            field.widget = widget
