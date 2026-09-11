from django import forms
from django.contrib import admin
from django.db import models
from unfold.admin import ModelAdmin
from configurator.models import ConfigurationExportTemplate
from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
from django.urls import path
from django.utils.safestring import mark_safe
from django.urls import reverse
from configurator.views import ExportTemplatePreviewView, ExportTemplateAIGenerateView
from django.utils.translation import gettext_lazy as _
from configurator.services.export.kind_registry import get_queryset_for_kind

@admin.register(ConfigurationExportTemplate)
class ConfigurationExportTemplateAdmin(ModelAdmin,CompanyRestrictedAdmin):
    list_display = ("name", "kind", "product", "is_active","company")
    list_filter = (("kind"), "is_active", ("product",AutocompleteSelectMultipleFilter))
    search_fields = ("name", "html")
    readonly_fields = ("visualizar", "contexto_json", "test_object_selector", "ai_assist")
    autocomplete_fields = ["product","company"]
    list_select_related = ["company"]

    formfield_overrides = {
        models.TextField: {
            "widget": forms.Textarea(
                attrs={
                    "rows": 28,
                    "style": "font-family: ui-monospace, monospace; font-size: 12px; width: 100%; max-width: 100%; box-sizing: border-box;",
                }
            )
        },
    }

    fieldsets = (
        ("Template Info", {
            "fields": (("name", "kind"), ("product", "is_active"), "company")
        }),
        (_("AI Assist"), {
            "fields": ("ai_assist",),
        }),
        ("Template Content", {
            "fields": ("html","css"),
            "classes": ("collapse",),
        }),
        (_("Visualizar"), {
            "fields": ("test_object_selector",),
        }),
        (None, {
            "fields": ("visualizar",),
        }),
        (_("Contexto JSON"), {
                    "fields": ("contexto_json",),
                    "classes": ("collapse",),
                }),
    )

    def visualizar(self, obj):
        # No pk-gate: the live preview URL (POST-based) already works from whatever is
        # currently in the form, saved or not - the iframe just starts blank pre-save
        # and gets populated by the JS-driven live refresh (on load, on edit, or after
        # an AI generation completes).
        preview_url = reverse("admin:configurator_export_template_preview", args=[obj.pk]) if obj and obj.pk else ""
        live_preview_url = reverse("admin:configurator_export_template_preview_live")
        # Full-width, its own row (below the collapsible Contexto JSON) so the user
        # always sees the rendered document. The frame shows the actual WeasyPrint
        # PDF (browser PDF viewer), not the intermediate HTML, so the preview matches
        # what gets emailed/downloaded. Wrapper uses Unfold border/radius tokens so
        # the frame matches the rest of the admin in light and dark mode.
        return mark_safe(
            f'<div class="border border-base-200 rounded-default overflow-hidden dark:border-base-800">'
            f'<iframe id="export-template-preview" src="{preview_url}" '
            f'data-live-preview-url="{live_preview_url}" '
            f'style="width: 100%; height: 800px; border: 0; background: white; display: block;"></iframe>'
            f'</div>'
        )
    
    visualizar.short_description = _("Visualizar")

    def contexto_json(self, obj):
        # Rendered inside a collapsible ("collapse") Unfold fieldset so this can be
        # folded away - the serialized context can run to hundreds of lines and the
        # user mostly only needs it when writing template expressions. Base styling
        # mirrors Unfold's own surfaces (base-50/base-900, base-200/base-800 borders);
        # max-h + overflow keeps a long payload from pushing the PDF preview off-screen.
        return mark_safe('''
            <div id="context-viewer-container"
                 class="border border-base-200 bg-base-50 rounded-default overflow-auto max-h-[480px]
                        dark:border-base-800 dark:bg-base-900">
                <pre id="context-json-content"
                     class="p-4 text-xs font-mono text-font-default-light whitespace-pre-wrap break-all
                            dark:text-font-default-dark"></pre>
            </div>
        ''')

    contexto_json.short_description = _("Contexto JSON")

    def test_object_selector(self, obj):
        # No pk-gate (see visualizar()). obj.kind/product/company reflect only the
        # model's defaults until first saved - imprecise for a brand-new record, but
        # "Latest (Default)" always works, and the actual preview/generation calls
        # read the live form fields via JS, not this dropdown's filtering.
        kind = obj.kind if obj else None
        product = obj.product if (obj and obj.product_id) else None
        company = obj.company if (obj and obj.company_id) else None
        qs = get_queryset_for_kind(kind, product=product, company=company) if kind else []
        latest_objects = qs.order_by('-id')[:10] if hasattr(qs, 'order_by') else []

        html = [f'<select id="test-object-selector" style="margin-bottom: 1rem; width: 100%; border-radius: 0.375rem; border: 1px solid #d1d5db; padding: 0.5rem;" class="bg-gray-50 border border-gray-300 text-gray-900 text-sm rounded-lg focus:ring-blue-500 focus:border-blue-500 block w-full p-2.5 dark:bg-gray-700 dark:border-gray-600 dark:placeholder-gray-400 dark:text-white dark:focus:ring-blue-500 dark:focus:border-blue-500">']
        html.append(f'<option value="">{_("Latest (Default)")}</option>')
        for item in latest_objects:
            html.append(f'<option value="{item.pk}">{item}</option>')
        html.append('</select>')
        
        return mark_safe("".join(html))

    test_object_selector.short_description = _("Objeto de prueba")

    # Real Unfold design tokens, pulled from its own button.html component and
    # widgets.py TEXTAREA_CLASSES/BASE_CLASSES - so this looks native (colors,
    # radii, dark mode) instead of a hand-styled approximation.
    _AI_BUTTON_BASE = (
        "font-medium inline-flex group items-center gap-1 relative rounded-default "
        "justify-center whitespace-nowrap cursor-pointer disabled:cursor-not-allowed "
        "disabled:opacity-50 px-3 py-2 text-sm border"
    )
    _AI_BUTTON_PRIMARY = _AI_BUTTON_BASE + " bg-primary-600 border-transparent text-white hover:bg-primary-600/80"
    _AI_BUTTON_DEFAULT = (
        _AI_BUTTON_BASE + " border-base-200 bg-white shadow-xs text-important "
        "dark:border-base-700 dark:bg-transparent hover:bg-base-100/80 dark:hover:bg-base-800/80"
    )
    _AI_TEXTAREA_CLASSES = (
        "border border-base-200 bg-white font-medium min-w-20 placeholder-base-400 "
        "rounded-default shadow-xs text-font-default-light text-sm focus:outline-2 "
        "focus:-outline-offset-2 focus:outline-primary-600 dark:bg-base-900 "
        "dark:border-base-700 dark:text-font-default-dark dark:scheme-dark "
        "disabled:!bg-base-50 dark:disabled:!bg-base-800 px-3 py-2 w-full appearance-none"
    )

    def ai_assist(self, obj):
        ai_generate_url = reverse("admin:configurator_export_template_ai_generate")
        return mark_safe(f'''
            <div id="ai-assist-container">
                <textarea id="ai-prompt-input" rows="3" class="{self._AI_TEXTAREA_CLASSES}"
                    placeholder="{_('Describe how you want the PDF to look, e.g. "make the total row bold and add the logo top right"')}"></textarea>
                <div class="flex items-center gap-3 mt-2 flex-wrap">
                    <button type="button" id="ai-generate-button" data-ai-generate-url="{ai_generate_url}" class="{self._AI_BUTTON_PRIMARY}">
                        {_("Generate with AI")}
                    </button>
                    <button type="button" id="ai-generate-generic-button" data-ai-generate-url="{ai_generate_url}" class="{self._AI_BUTTON_DEFAULT}">
                        {_("Generate Generic PDF")}
                    </button>
                    <span id="ai-generate-status" class="text-xs text-subtle flex items-center gap-1"></span>
                </div>
                <p class="text-xs text-subtle mt-1.5">
                    {_('"Generate Generic PDF" builds a clean default layout with no prompt needed - "Generate with AI" applies your instruction above.')}
                </p>
            </div>
        ''')

    ai_assist.short_description = _("AI Assist")

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
            path(
                "ai-generate/",
                self.admin_site.admin_view(ExportTemplateAIGenerateView.as_view()),
                name="configurator_export_template_ai_generate",
            ),
        ]
        return custom_urls + urls

    class Media:
        js = (
            "js/admin/export_template_visualizer.js",
        )

