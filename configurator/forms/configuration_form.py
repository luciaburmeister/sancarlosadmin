from django import forms
from configurator.models import ParameterType, ConfigurationParameterValue, Configuration, ConfigurationStatus
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminSelectWidget,
)


class ConfigurationForm(forms.Form):
    """
    A dynamic form that builds fields based on product parameters.
    """

    def __init__(self, *args, product=None, **kwargs):
        self.product = product
        super().__init__(*args, **kwargs)

        if not product:
            raise ValueError("ConfigurationForm requires a product.")

        for param in product.parameters.all():
            field_name = f"param_{param.id}"

            # NUMBER → FloatField w/ decimal widget
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

            # Boolean handling
            if param.type == ParameterType.BOOLEAN and value is None:
                value = False

            ConfigurationParameterValue.objects.create(
                configuration=config,
                parameter=param,
                value=str(value),
            )

        config.generate_bom()
        config.recalculate_price()

        return config
