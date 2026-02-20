from django.contrib import admin
from unfold.admin import ModelAdmin
from django.db import models
from configurator.models import ConfigurationExportTemplate
from unfold.contrib.forms.widgets import WysiwygWidget
from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
from django.urls import path
from django.utils.safestring import mark_safe
from django.urls import reverse
from configurator.views import ExportTemplatePreviewView
from django.utils.translation import gettext_lazy as _

@admin.register(ConfigurationExportTemplate)
class ConfigurationExportTemplateAdmin(ModelAdmin,CompanyRestrictedAdmin):
    list_display = ("name", "kind", "product", "is_active","company")
    list_filter = (("kind"), "is_active", ("product",AutocompleteSelectMultipleFilter))
    search_fields = ("name", "html")
    readonly_fields = ("visualizar",)
    autocomplete_fields = ["product","company"]
    formfield_overrides = {
        models.TextField: {
            "widget": WysiwygWidget,
        }
    }
    list_select_related = ["company"]

    fieldsets = (
        ("Template Info", {
            "fields": (("name", "kind"), ("product", "is_active"), "company")
        }),
        ("Template Content", {
            "fields": ("html","css"),
        }),
        (_("Visualizar"), {
            "fields": ("visualizar",),
        }),
    )

    def visualizar(self, obj):
        if not obj.pk:
            return _("Save the template first to see a preview.")
        
        preview_url = reverse("admin:configurator_export_template_preview", args=[obj.pk])
        live_preview_url = reverse("admin:configurator_export_template_preview_live")
        return mark_safe(f'<iframe id="export-template-preview" src="{preview_url}" data-live-preview-url="{live_preview_url}" style="width: 100%; height: 600px; border: 1px solid #ccc; border-radius: 8px; background: white;"></iframe>')
    
    visualizar.short_description = _("Visualizar")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "preview/<int:template_id>/",
                self.admin_site.admin_view(ExportTemplatePreviewView.as_view(model_admin=self)),
                name="configurator_export_template_preview",
            ),
            path(
                "preview/live/",
                self.admin_site.admin_view(ExportTemplatePreviewView.as_view(model_admin=self)),
                name="configurator_export_template_preview_live",
            ),
        ]
        return custom_urls + urls

    class Media:
        js = (
            "js/admin/export_template_visualizer.js",
        )

