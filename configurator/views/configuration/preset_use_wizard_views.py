from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from formtools.wizard.views import SessionWizardView
from unfold.views import UnfoldModelAdminViewMixin

from configurator.models import ConfigurationPreset
from configurator.services.preset_service import create_configuration_from_preset
from configurator.forms.preset_use_forms import (
    PresetClientLinkForm,
    PresetClientLinkFormHelper,
    PresetMissingParamsForm,
    PresetMissingParamsFormHelper,
)
from sales.models import Quote


def _has_missing_params(wizard):
    """Condition function: show the missing-params step only if needed."""
    return bool(wizard.preset.get_missing_parameters())


def _needs_client_link(wizard):
    """Skip the client_link step when quote_id is provided via GET."""
    return not getattr(wizard, "quote_from_url", None)


class PresetUseWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    """
    Use-preset wizard launched from the action row or from a Quote's preset picker.

    Normal flow (from Preset admin):
        Step 1: Link quote / client
        Step 2 (conditional): Fill missing parameter values

    Quote flow (with ?quote_id=X):
        Step 1 is skipped (quote is known).
        If no missing params → configuration created instantly, redirect back to quote.
        If missing params → only the missing-params step is shown.
    """

    form_list = [
        ("client_link", PresetClientLinkForm),
        ("missing_params", PresetMissingParamsForm),
    ]
    condition_dict = {
        "client_link": _needs_client_link,
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

        # Check for quote_id from GET (quote preset picker) or session
        quote_id = request.GET.get("quote_id") or request.session.get("preset_wizard_quote_id")
        self.quote_from_url = None
        if quote_id:
            self.quote_from_url = Quote.objects.filter(pk=quote_id).first()
            # Persist in session so it survives POST steps
            request.session["preset_wizard_quote_id"] = str(quote_id)

        # If quote is known and no missing params → create directly, skip wizard entirely
        if self.quote_from_url and not self.preset.get_missing_parameters():
            config = create_configuration_from_preset(
                preset=self.preset,
                quote=self.quote_from_url,
                merged_values=dict(self.preset.parameter_values),
            )
            request.session.pop("preset_wizard_quote_id", None)
            return redirect(
                reverse("admin:sales_quote_change", args=[self.quote_from_url.pk])
            )

        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if "quote_id" in request.GET:
            request.session["preset_wizard_quote_id"] = request.GET["quote_id"]
        return super().get(request, *args, **kwargs)

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
        client_form = form_dict.get("client_link")
        missing_form = form_dict.get("missing_params")

        # Determine quote: from URL context or from the client_link form
        if self.quote_from_url:
            quote = self.quote_from_url
            asset = None
        else:
            quote = client_form.cleaned_data.get("quote") if client_form else None
            asset = client_form.cleaned_data.get("asset") if client_form else None

        # Merge parameter values: preset values + user-supplied missing values
        merged_values = dict(self.preset.parameter_values)

        if missing_form:
            for field_name, value in missing_form.cleaned_data.items():
                if not field_name.startswith("param_"):
                    continue
                field = missing_form.fields[field_name]
                param_name = getattr(field, "parameter_name", field_name)
                if value is not None and value != "":
                    merged_values[param_name] = str(value) if not isinstance(value, bool) else value

        config = create_configuration_from_preset(
            preset=self.preset,
            quote=quote,
            merged_values=merged_values,
            asset=asset,
        )

        # Clean up session
        self.request.session.pop("preset_wizard_quote_id", None)

        # Redirect back to quote if launched from quote context, else to config
        if self.quote_from_url and quote:
            return redirect(
                reverse("admin:sales_quote_change", args=[quote.pk])
            )

        return redirect(
            reverse(
                "admin:configurator_configuration_change",
                args=[config.id],
            )
        )
