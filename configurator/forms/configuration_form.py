from django import forms
from configurator.models import ParameterType,Configuration,ConfigurationStatus,ConfigurationParameterValue
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminSelectWidget,
)
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column
from django.forms import CheckboxInput

class ConfigurationFormHelper(FormHelper):
    def __init__(self, form, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.form = form
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"

        self.layout = Layout(*self.build_rows())
    
    def build_rows(self):
        rows = []
        buffer = []

        for name, field in self.form.fields.items():
            widget = field.widget

            # Checkboxes → full-width row
            if isinstance(widget, CheckboxInput):
                rows.append(
                    Row(
                        Column(
                            Field(name),
                            css_class="col-span-12"
                        ),
                        css_class="mb-3"
                    )
                )
                continue

            buffer.append(name)

            # Flush 2-column row
            if len(buffer) == 2:
                rows.append(self.two_col_row(buffer))
                buffer = []

        # Remaining single field
        if buffer:
            rows.append(self.one_col_row(buffer[0]))

        return rows
    
    def two_col_row(self, names):
        return Row(
            *[
                Column(
                    Field(name),
                    css_class="col-span-12 md:col-span-6"
                )
                for name in names
            ],
            css_class="gap-4 mb-4"
        )

    def one_col_row(self, name):
        return Row(
            Column(
                Field(name),
                css_class="col-span-12"
            ),
            css_class="mb-4"
        )



class ConfigurationForm(forms.Form):
    """
    Dynamically builds fields based on the product's parameters.
    """

    def __init__(self, *args, product=None, **kwargs):
        self.product = product
        super().__init__(*args, **kwargs)

        if not product:
            raise ValueError("ConfigurationForm requires a product.")

        # Build fields dynamically
        for param in product.parameters.all():
            field_name = f"param_{param.id}"

            if param.type == ParameterType.NUMBER:
                field = forms.FloatField(
                    label=param.label,
                    required=param.required,
                    widget=UnfoldAdminDecimalFieldWidget(),
                )

            elif param.type == ParameterType.TEXT:
                field = forms.CharField(
                    label=param.label,
                    required=param.required,
                    max_length=200,
                    widget=UnfoldAdminTextInputWidget(),
                )

            elif param.type == ParameterType.BOOLEAN:
                field = forms.BooleanField(
                    label=param.label,
                    required=False,
                )

            elif param.type == ParameterType.SELECT:
                if isinstance(param.options, list):
                    choices = [(o, o) for o in param.options]
                else:
                    choices = list(param.options.items()) if param.options else []

                field = forms.ChoiceField(
                    label=param.label,
                    required=param.required,
                    choices=choices,
                    widget=UnfoldAdminSelectWidget(),
                )

            self.fields[field_name] = field


    def save(self):
        """Create the Configuration + Parameter values."""
        product = self.product

        config = Configuration.objects.create(
            name=f"{product.name} Configuration",
            product=product,
            status=ConfigurationStatus.DRAFT,
            client=None,
        )

        for param in product.parameters.all():
            field_name = f"param_{param.id}"
            value = self.cleaned_data.get(field_name)

            if param.type == ParameterType.BOOLEAN and value is None:
                value = False

            ConfigurationParameterValue.objects.create(
                configuration=config,
                parameter=param,
                value=str(value),
                order = param.order
            )

        config.generate_bom()
        config.recalculate_price()

        return config
