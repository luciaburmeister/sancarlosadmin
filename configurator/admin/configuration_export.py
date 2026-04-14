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
    readonly_fields = ("visualizar", "contexto_json", "test_object_selector")
    autocomplete_fields = ["product","company"]
    list_select_related = ["company"]

    fieldsets = (
        ("Template Info", {
            "fields": (("name", "kind"), ("product", "is_active"), "company")
        }),
        ("Template Content", {
            "fields": ("html","css"),
        }),
        (_("Visualizar"), {
            "fields": ("test_object_selector", ("visualizar", "contexto_json"),),
        }),
    )

    def visualizar(self, obj):
        if not obj.pk:
            return _("Save the template first to see a preview.")
        
        preview_url = reverse("admin:configurator_export_template_preview", args=[obj.pk])
        live_preview_url = reverse("admin:configurator_export_template_preview_live")
        return mark_safe(f'<iframe id="export-template-preview" src="{preview_url}" data-live-preview-url="{live_preview_url}" style="width: 100%; height: 600px; border: 1px solid #ccc; border-radius: 8px; background: white;"></iframe>')
    
    visualizar.short_description = _("Visualizar")

    def contexto_json(self, obj):
        return mark_safe(f'''
            <div id="context-viewer-container" class="p-4 bg-gray-50 border border-gray-200 rounded-lg dark:bg-gray-800 dark:border-gray-700 h-[600px] overflow-auto">
                <div id="context-json-display" class="text-xs font-mono">
                    <pre id="context-json-content"></pre>
                </div>
            </div>
        ''')
    
    contexto_json.short_description = _("Contexto JSON")

    def test_object_selector(self, obj):
        if not obj.pk:
            return _("Save the template first.")
            
        from configurator.models import Configuration, ExportTemplateKind
        from sales.models import Quote
        from production.models import ProductionItem
        from invoices.models import SalesInvoice

        qs = []
        if obj.kind == ExportTemplateKind.QUOTATION:
            qs = Configuration.objects.all()
            if obj.product:
                qs = qs.filter(product=obj.product)
        elif obj.kind == ExportTemplateKind.PRODUCTION_ORDER:
            qs = Configuration.objects.all()
            if obj.product:
                qs = qs.filter(product=obj.product)
        elif obj.kind == ExportTemplateKind.PRODUCTION_TICKET:
            qs = ProductionItem.objects.all()
        elif obj.kind == ExportTemplateKind.SALES_INVOICE:
            qs = SalesInvoice.objects.all()
        elif obj.kind == ExportTemplateKind.CONSOLIDATED_QUOTATION:
            qs = Quote.objects.all()
        elif obj.kind == ExportTemplateKind.RECEIVAL_SLIP:
            from configurator.models import ReceivalSlip
            qs = ReceivalSlip.objects.all()
            if obj.product:
                qs = qs.filter(configuration__product=obj.product)

        if hasattr(qs, 'filter') and obj.company:
            qs = qs.filter(company=obj.company)
            
        latest_objects = qs.order_by('-id')[:10] if hasattr(qs, 'order_by') else []

        html = [f'<select id="test-object-selector" style="margin-bottom: 1rem; width: 100%; border-radius: 0.375rem; border: 1px solid #d1d5db; padding: 0.5rem;" class="bg-gray-50 border border-gray-300 text-gray-900 text-sm rounded-lg focus:ring-blue-500 focus:border-blue-500 block w-full p-2.5 dark:bg-gray-700 dark:border-gray-600 dark:placeholder-gray-400 dark:text-white dark:focus:ring-blue-500 dark:focus:border-blue-500">']
        html.append(f'<option value="">{_("Latest (Default)")}</option>')
        for item in latest_objects:
            html.append(f'<option value="{item.pk}">{item}</option>')
        html.append('</select>')
        
        return mark_safe("".join(html))

    test_object_selector.short_description = _("Objeto de prueba")

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

