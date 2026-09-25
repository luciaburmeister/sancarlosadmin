from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from company.active_company import ActiveCompany
from configurator.models import PricingRule, ProductTemplate
from core.admin.company_admin import CompanyRestrictedAdmin


# Aggregates that Configuration.get_pricing_context() always provides,
# regardless of the product template.
BUILTIN_PRICING_VARIABLES = (
    ("base_total", _("Running subtotal (updated after each rule)")),
    ("material_cost", _("Sum of component cost × quantity")),
    ("quantity", _("Total quantity of configuration items")),
    ("item_count", _("Number of configuration items")),
)


@admin.register(PricingRule)
class PricingRuleAdmin(ModelAdmin,CompanyRestrictedAdmin):
    list_display = ("id", "name", "expression", "is_active","company")
    list_filter = ( "is_active",)
    search_fields = ("name", "expression")
    autocomplete_fields = ["company"]
    list_select_related = ["company"]
    readonly_fields = ("available_variables",)

    fieldsets = (
        ("Pricing Rule", {
            "fields": (("company"), "name","label", "expression", ("is_active","show_in_export")),
        }),
        (_("Available variables"), {
            "fields": ("available_variables",),
            "classes": ("collapse",),
            "description": _(
                "Every name below can be used in the expression. Parameters and "
                "computed variables depend on the product template the rule is "
                "applied to, so only use them in rules assigned to that product."
            ),
        }),
    )

    # ------------------------------------------------------------------
    # Variable reference panel
    # ------------------------------------------------------------------

    def _product_templates_for(self, obj):
        """
        Product templates whose parameters/computed variables are shown.

        Scoped to the rule's company when the rule has one, otherwise to the
        companies currently active in the switcher (all templates outside a
        request, e.g. in tests).
        """
        qs = ProductTemplate.objects.prefetch_related("parameters", "computed_vars")
        if obj is not None and obj.company_id:
            qs = qs.filter(company_id=obj.company_id)
        else:
            active_ids = ActiveCompany.get_current_ids()
            if active_ids is not None:
                qs = qs.filter(company_id__in=active_ids)
        return qs.order_by("name")

    @staticmethod
    def _render_table(rows):
        """rows: iterable of (code, description) pairs -> Unfold-styled table."""
        return format_html(
            '<table class="w-full text-sm">'
            "<tbody>{}</tbody></table>",
            format_html_join(
                "",
                '<tr class="border-b border-base-200 last:border-0 dark:border-base-800">'
                '<td class="py-1 pr-4 align-top whitespace-nowrap">'
                '<code class="font-mono text-xs">{}</code></td>'
                '<td class="py-1 text-subtle align-top">{}</td></tr>',
                rows,
            ),
        )

    def _render_product_section(self, product):
        parameters = list(product.parameters.all())
        computed = list(product.computed_vars.all())
        if not parameters and not computed:
            return ""

        blocks = []
        if parameters:
            blocks.append(
                format_html(
                    '<div class="mb-2"><div class="text-xs font-semibold uppercase '
                    'text-subtle mb-1">{}</div>{}</div>',
                    _("Parameters"),
                    self._render_table(
                        (p.name, f"{p.label} ({p.get_type_display()})")
                        for p in parameters
                    ),
                )
            )
        if computed:
            blocks.append(
                format_html(
                    '<div><div class="text-xs font-semibold uppercase '
                    'text-subtle mb-1">{}</div>{}</div>',
                    _("Computed variables"),
                    self._render_table((v.name, v.expression) for v in computed),
                )
            )

        return format_html(
            '<details class="border border-base-200 rounded-default mb-2 '
            'dark:border-base-800">'
            '<summary class="cursor-pointer px-3 py-2 font-medium select-none">'
            "{}</summary>"
            '<div class="px-3 pb-3">{}</div></details>',
            product.name,
            format_html_join("", "{}", ((b,) for b in blocks)),
        )

    @admin.display(description=_("Available variables"))
    def available_variables(self, obj=None):
        builtin = format_html(
            '<div class="mb-4"><div class="text-xs font-semibold uppercase '
            'text-subtle mb-1">{}</div>{}</div>',
            _("Always available"),
            self._render_table(BUILTIN_PRICING_VARIABLES),
        )

        sections = [
            self._render_product_section(product)
            for product in self._product_templates_for(obj)
        ]
        sections = [s for s in sections if s]

        if sections:
            products = format_html(
                '<div><div class="text-xs font-semibold uppercase text-subtle mb-1">'
                "{}</div>{}</div>",
                _("By product template"),
                format_html_join("", "{}", ((s,) for s in sections)),
            )
        else:
            products = format_html(
                '<p class="text-subtle text-sm">{}</p>',
                _("No product template defines parameters or computed variables yet."),
            )

        return format_html(
            '<div class="border border-base-200 bg-base-50 rounded-default p-4 '
            'overflow-auto max-h-[480px] dark:border-base-800 dark:bg-base-900">'
            "{}{}</div>",
            builtin,
            products,
        )
