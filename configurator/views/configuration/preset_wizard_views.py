from django.shortcuts import redirect, get_object_or_404
from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView
from formtools.wizard.views import SessionWizardView
from unfold.views import UnfoldModelAdminViewMixin

from configurator.models import ProductTemplate, ConfigurationPreset
from configurator.forms.preset_wizard_forms import (
    PresetBasicForm,
    PresetBasicFormHelper,
    PresetParameterForm,
    PresetParameterFormHelper,
)
from core.views.company_queryset import CompanyRestrictedQuerysetMixin


class PresetProductSelectionView(
    UnfoldModelAdminViewMixin, CompanyRestrictedQuerysetMixin, ListView
):
    """Reuses the product selection pattern to pick which product to create a preset for."""

    title = _("Select Product for Preset")
    permission_required = ("configurator.add_configurationpreset",)
    template_name = "configurator/product_selection.html"
    model = ProductTemplate
    paginate_by = 20
    context_object_name = "products"

    def get_queryset(self):
        qs = super().get_queryset().filter(show_in_configurator=True)
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['search_query'] = self.request.GET.get('q', '')
        # Override the URL target so product cards link to the preset wizard
        context["wizard_url_name"] = (
            "admin:configurator_configurationpreset_wizard_form"
        )
        return context


class PresetCreationWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    """Two-step wizard: basic info → parameter values."""

    form_list = [
        ("basic", PresetBasicForm),
        ("parameters", PresetParameterForm),
    ]
    template_name = "configurator/wizard_form.html"
    permission_required = ("configurator.add_configurationpreset",)
    title = _("Create Configuration Preset")

    def dispatch(self, request, product_id=None, *args, **kwargs):
        self.product = get_object_or_404(ProductTemplate, pk=product_id)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        kwargs["product"] = self.product
        kwargs["request"] = self.request
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["product"] = self.product

        if self.steps.current == "basic":
            context["helper"] = PresetBasicFormHelper()
        elif self.steps.current == "parameters":
            form = self.get_form(step=self.steps.current)
            context["helper"] = PresetParameterFormHelper(form)

        return context

    def done(self, form_list, **kwargs):
        basic_form = form_list[0]
        param_form = form_list[1]

        # Build parameter_values JSON from submitted data
        parameter_values = {}
        for field_name, value in param_form.cleaned_data.items():
            if not field_name.startswith("param_"):
                continue
            # Skip empty / None values — those will be asked during usage
            if value is None or value == "" or value == []:
                continue
            # Get the parameter name from the field attribute
            field = param_form.fields[field_name]
            param_name = getattr(field, "parameter_name", field_name)
            parameter_values[param_name] = str(value) if not isinstance(value, bool) else value

        preset = ConfigurationPreset.objects.create(
            product=self.product,
            company=self.product.company,
            name=basic_form.cleaned_data["name"],
            description=basic_form.cleaned_data.get("description", ""),
            is_active=basic_form.cleaned_data.get("is_active", True),
            parameter_values=parameter_values,
        )

        return redirect(
            reverse(
                "admin:configurator_configurationpreset_change",
                args=[preset.pk],
            )
        )
