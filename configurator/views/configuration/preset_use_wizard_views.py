from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from formtools.wizard.views import SessionWizardView
from unfold.views import UnfoldModelAdminViewMixin

from configurator.models import (
    Configuration,
    ConfigurationPreset,
    ConfigurationParameterValue,
    ConfigurationStatus,
)
from configurator.forms.preset_use_forms import (
    PresetClientLinkForm,
    PresetClientLinkFormHelper,
    PresetMissingParamsForm,
    PresetMissingParamsFormHelper,
)


def _has_missing_params(wizard):
    """Condition function: show the missing-params step only if needed."""
    return bool(wizard.preset.get_missing_parameters())


class PresetUseWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    """
    Use-preset wizard launched from the action row.
    Step 1: Link quote / client
    Step 2 (conditional): Fill missing parameter values
    """

    form_list = [
        ("client_link", PresetClientLinkForm),
        ("missing_params", PresetMissingParamsForm),
    ]
    condition_dict = {
        "missing_params": _has_missing_params,
    }
    template_name = "configurator/wizard_form.html"
    permission_required = ("configurator.view_configurationpreset",)
    title = _("Create Configuration from Preset")

    def dispatch(self, request, preset_id=None, *args, **kwargs):
        self.preset = get_object_or_404(
            ConfigurationPreset.objects.select_related("product"),
            pk=preset_id,
            is_active=True,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        kwargs["preset"] = self.preset
        kwargs["request"] = self.request
        kwargs["product"] = self.preset.product
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["product"] = self.preset.product
        context["preset"] = self.preset

        if self.steps.current == "client_link":
            context["helper"] = PresetClientLinkFormHelper()
        elif self.steps.current == "missing_params":
            form = self.get_form(step=self.steps.current)
            context["helper"] = PresetMissingParamsFormHelper(form)

        return context

    def done(self, form_list, form_dict=None, **kwargs):
        client_form = form_dict["client_link"]
        missing_form = form_dict.get("missing_params")

        quote = client_form.cleaned_data.get("quote")
        client = quote.client if quote else None
        asset = client_form.cleaned_data.get("asset")

        product = self.preset.product

        # 1. Create Configuration
        config = Configuration.objects.create(
            name=f"{self.preset.name}",
            product=product,
            status=ConfigurationStatus.DRAFT,
            client=client,
            asset=asset,
            company=product.company,
            quote=quote,
        )

        # 2. Merge parameter values: preset values + user-supplied missing values
        merged_values = dict(self.preset.parameter_values)

        if missing_form:
            for field_name, value in missing_form.cleaned_data.items():
                if not field_name.startswith("param_"):
                    continue
                field = missing_form.fields[field_name]
                param_name = getattr(field, "parameter_name", field_name)
                if value is not None and value != "":
                    merged_values[param_name] = str(value) if not isinstance(value, bool) else value

        # 3. Create ConfigurationParameterValue records
        for param in product.parameters.all():
            raw_value = merged_values.get(param.name)
            if raw_value is None:
                # Use default value from parameter if available
                raw_value = param.default_value or ""

            ConfigurationParameterValue.objects.create(
                configuration=config,
                parameter=param,
                value=str(raw_value),
                order=param.order,
            )

        # 4. Setup defaults from product
        config.copy_default_pricing_rules_from_product()
        config.copy_default_taxes_from_product()

        # 5. Generate BOM and recalculate price
        config.generate_bom()
        config.recalculate_price()

        return redirect(
            reverse(
                "admin:configurator_configuration_change",
                args=[config.id],
            )
        )

