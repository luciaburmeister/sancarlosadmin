from django.contrib import admin
from django.db import models
from django.urls import path, reverse
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from django_json_widget.widgets import JSONEditorWidget
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
)
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import (
    ImportForm,
    SelectableFieldsExportForm,
)
from core.admin.company_admin import CompanyRestrictedAdmin
from configurator.models import ConfigurationPreset
from configurator.resources.preset_resource import ConfigurationPresetResource
from configurator.views.configuration.preset_wizard_views import (
            PresetProductSelectionView,
            PresetCreationWizardView,
        )
from configurator.views.configuration.preset_use_wizard_views import (
            PresetUseWizardView,
        )

from unfold.views import BaseAutocompleteView
from core.admin.company_access import get_user_companies
from django.db.models import Q


class ConfigurationPresetAutocompleteView(BaseAutocompleteView):
    model = ConfigurationPreset

    def get_queryset(self):
        qs = super().get_queryset().filter(is_active=True).select_related("product")
        user = self.request.user

        if not user.is_superuser:
            qs = qs.filter(product__company__in=get_user_companies(user))

        term = self.request.GET.get("term")
        if term:
            qs = qs.filter(
                Q(name__icontains=term)
                | Q(reference_code__icontains=term)
                | Q(product__name__icontains=term)
            )

        return qs.order_by("order", "name")


@admin.register(ConfigurationPreset)
class ConfigurationPresetAdmin(CompanyRestrictedAdmin, ModelAdmin, ImportExportModelAdmin):
    custom_urls = (
        (
            "autocomplete/preset/",
            "preset_autocomplete",
            ConfigurationPresetAutocompleteView.as_view(),
        ),
    )
    resource_classes = [ConfigurationPresetResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm

    list_display = (
        "reference_code",
        "name",
        "product",
        "final_price",
        "company",
        "is_active",
        "order",
    )

    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("product", AutocompleteSelectMultipleFilter),
        "is_active",
    )

    search_fields = ("name", "reference_code", "product__name")
    autocomplete_fields = ("product",)
    list_select_related = ("product", "company")
    list_filter_submit = True

    actions_row = ["use_preset_action"]

    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }

    fieldsets = (
        (
            _("Preset Information"),
            {
                "fields": (
                    ("name", "reference_code"),
                    "product",
                    "description",
                    "image",
                    ("is_active", "order"),
                    "final_price",
                )
            },
        ),
        (
            _("Parameter Values"),
            {
                "fields": ("parameter_values",),
                "description": _(
                    "JSON map of parameter name → value. "
                    "Parameters not listed here will be asked during usage."
                ),
            },
        ),
    )

    @action(description=_("Use Preset"), icon="play_arrow")
    def use_preset_action(self, request, object_id):
        url = reverse(
            "admin:configurator_configurationpreset_use",
            args=[object_id],
        )
        return redirect(url)

    def get_urls(self):
        urls = super().get_urls()
        

        custom = [
            path(
                "wizard/create/",
                self.admin_site.admin_view(
                    PresetProductSelectionView.as_view(model_admin=self)
                ),
                name="configurator_configurationpreset_wizard_select",
            ),
            path(
                "wizard/create/<int:product_id>/",
                self.admin_site.admin_view(
                    PresetCreationWizardView.as_view(model_admin=self)
                ),
                name="configurator_configurationpreset_wizard_form",
            ),
            path(
                "<int:preset_id>/use/",
                self.admin_site.admin_view(
                    PresetUseWizardView.as_view(model_admin=self)
                ),
                name="configurator_configurationpreset_use",
            ),
        ]
        return custom + urls

    def add_view(self, request, form_url="", extra_context=None):
        """Redirect the default 'Add' button to the creation wizard."""
        return redirect(
            reverse("admin:configurator_configurationpreset_wizard_select")
        )
