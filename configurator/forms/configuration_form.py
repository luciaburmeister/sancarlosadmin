from django import forms
from configurator.models import ParameterType,Configuration,ConfigurationStatus,ConfigurationParameterValue
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminSelectWidget,
)
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column,HTML
from django.forms import CheckboxInput
from django.utils.translation import gettext_lazy as _

class ConfigurationFormHelper(FormHelper):
    def __init__(self, form, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.form = form
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"

        self.layout = Layout(*self.build_grouped_rows())

    def build_grouped_rows(self):
        rows = []
        grouped_fields = {}

        for name, field in self.form.fields.items():
            group = getattr(field, "parameter_group", None) or _("General")
            grouped_fields.setdefault(group, []).append(name)

        for group, field_names in grouped_fields.items():
            # Open group container
            rows.append(
                HTML(
                    f"""
                    <div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                        <h3 class="text-base font-semibold text-gray-800 mb-3">
                            {group}
                        </h3>
                    """
                )
            )

            buffer = []

            for name in field_names:
                widget = self.form.fields[name].widget

                if isinstance(widget, CheckboxInput):
                    rows.append(
                        Row(
                            Column(Field(name), css_class="col-span-12"),
                            css_class="mb-3"
                        )
                    )
                    continue

                buffer.append(name)

                if len(buffer) == 2:
                    rows.append(self.two_col_row(buffer))
                    buffer = []

            if buffer:
                rows.append(self.one_col_row(buffer[0]))

            # Close group container
            rows.append(HTML("</div>"))

        return rows


    def two_col_row(self, names):
        return Row(
            *[
                Column(Field(name), css_class="col-span-12 md:col-span-6")
                for name in names
            ],
            css_class="gap-4 mb-4"
        )

    def one_col_row(self, name):
        return Row(
            Column(Field(name), css_class="col-span-12"),
            css_class="mb-4"
        )



class ConfigurationForm(forms.Form):
    """
    Dynamically builds fields based on the product's parameters.
    """

    def __init__(self, *args, product=None, **kwargs):
        self.product = product
        self.request = kwargs.pop("request", None)
    
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
                    initial=param.default_value,
                    widget=UnfoldAdminDecimalFieldWidget(),
                )

            elif param.type == ParameterType.TEXT:
                field = forms.CharField(
                    label=param.label,
                    required=param.required,
                    initial=param.default_value,
                    max_length=200,
                    widget=UnfoldAdminTextInputWidget(),
                )

            elif param.type == ParameterType.BOOLEAN:
                field = forms.BooleanField(
                    label=param.label,
                    required=False,
                    initial=param.default_value == 'true' if isinstance(param.default_value, str) else bool(param.default_value),
                )

            elif param.type == ParameterType.SELECT:
                if isinstance(param.options, list):
                    choices = [(o, o) for o in param.options]
                else:
                    choices = list(param.options.items()) if param.options else []

                field = forms.ChoiceField(
                    label=param.label,
                    required=param.required,
                    initial=param.default_value,
                    choices=choices,
                    widget=UnfoldAdminSelectWidget(),
                )
            

            field.parameter_group = param.parameter_group or _("General")
            self.fields[field_name] = field
            


    def save(self, client=None, asset=None):
        """Create the Configuration + Parameter values."""
        product = self.product

        config = Configuration.objects.create(
            name=f"{product.name} Configuration",
            product=product,
            status=ConfigurationStatus.DRAFT,
            client=client,
            asset=asset,
            company=product.company
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
