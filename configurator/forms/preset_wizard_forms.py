from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column, HTML
from unfold.widgets import (
    UnfoldAdminTextInputWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminSelectWidget,
)
from django.forms import CheckboxInput
from configurator.models import ParameterType


class PresetBasicForm(forms.Form):
    """Step 1 of the creation wizard: basic preset info."""

    name = forms.CharField(
        label=_("Preset Name"),
        max_length=200,
        widget=UnfoldAdminTextInputWidget(),
    )
    description = forms.CharField(
        label=_("Description"),
        required=False,
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 3}),
    )
    is_active = forms.BooleanField(
        label=_("Active"),
        required=False,
        initial=True,
    )

    def __init__(self, *args, product=None, request=None, **kwargs):
        self.product = product
        super().__init__(*args, **kwargs)
        self.helper = PresetBasicFormHelper()


class PresetBasicFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(
            HTML(
                f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                    <h3 class="text-base font-semibold text-gray-800 mb-3">
                        {_('Preset Information')}
                    </h3>"""
            ),
            Row(
                Column(Field("name"), css_class="col-span-12 md:col-span-8"),
                Column(Field("is_active"), css_class="col-span-12 md:col-span-4"),
                css_class="gap-4 mb-4",
            ),
            Row(
                Column(Field("description"), css_class="col-span-12"),
                css_class="mb-4",
            ),
            HTML("</div>"),
        )


class PresetParameterForm(forms.Form):
    """
    Step 2 of the creation wizard: parameter values.
    Dynamically built from the product's ProductParameter definitions.
    ALL fields are optional — unfilled fields will be asked during usage.
    """

    def __init__(self, *args, product=None, request=None, **kwargs):
        self.product = product
        super().__init__(*args, **kwargs)

        if not self.product:
            return

        for param in self.product.parameters.all():
            field_name = f"param_{param.id}"

            if param.type == ParameterType.NUMBER:
                field = forms.FloatField(
                    label=param.label,
                    required=False,
                    initial=param.default_value,
                    widget=UnfoldAdminDecimalFieldWidget(),
                )
            elif param.type == ParameterType.TEXT:
                field = forms.CharField(
                    label=param.label,
                    required=False,
                    initial=param.default_value,
                    max_length=200,
                    widget=UnfoldAdminTextInputWidget(),
                )
            elif param.type == ParameterType.BOOLEAN:
                val = param.default_value
                if isinstance(val, str):
                    val = val.lower() == "true"
                else:
                    val = bool(val) if val else False

                field = forms.BooleanField(
                    label=param.label,
                    required=False,
                    initial=val,
                )
            elif param.type == ParameterType.SELECT:
                if isinstance(param.options, list):
                    choices = [("", _("— Leave empty —"))] + [
                        (o, o) for o in param.options
                    ]
                else:
                    choices = [("", _("— Leave empty —"))] + (
                        list(param.options.items()) if param.options else []
                    )

                field = forms.ChoiceField(
                    label=param.label,
                    required=False,
                    initial=param.default_value,
                    choices=choices,
                    widget=UnfoldAdminSelectWidget(),
                )
            else:
                continue

            field.parameter_group = param.parameter_group or _("General")
            field.parameter_name = param.name
            self.fields[field_name] = field


class PresetParameterFormHelper(FormHelper):
    def __init__(self, form, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form = form
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(*self._build_grouped_rows())

    def _build_grouped_rows(self):
        rows = []
        grouped_fields = {}

        for name, field in self.form.fields.items():
            group = getattr(field, "parameter_group", None) or _("General")
            grouped_fields.setdefault(group, []).append(name)

        for group, field_names in grouped_fields.items():
            rows.append(
                HTML(
                    f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                        <h3 class="text-base font-semibold text-gray-800 mb-3">{group}</h3>
                        <p class="text-xs text-gray-500 mb-3">{_('Leave fields empty to ask the user during usage.')}</p>"""
                )
            )

            buffer = []
            for name in field_names:
                widget = self.form.fields[name].widget
                if isinstance(widget, CheckboxInput):
                    rows.append(
                        Row(
                            Column(Field(name), css_class="col-span-12"),
                            css_class="mb-3",
                        )
                    )
                    continue

                buffer.append(name)
                if len(buffer) == 2:
                    rows.append(
                        Row(
                            *[
                                Column(Field(n), css_class="col-span-12 md:col-span-6")
                                for n in buffer
                            ],
                            css_class="gap-4 mb-4",
                        )
                    )
                    buffer = []

            if buffer:
                rows.append(
                    Row(
                        Column(Field(buffer[0]), css_class="col-span-12"),
                        css_class="mb-4",
                    )
                )

            rows.append(HTML("</div>"))

        return rows
