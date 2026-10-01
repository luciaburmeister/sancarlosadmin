from django.contrib import admin, messages
from unfold.admin import ModelAdmin
from configurator.models import Configuration, ConfigurationParameterValue, ConfigurationItem, PricingRuleAssignment, ConfigurationFlow,ConfigurationStatus,ConfigurationPlanningStatus,ConfigurationComponentGroup
from sales.models import QuoteStatus
from unfold.admin import TabularInline,GenericTabularInline,StackedInline
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter,RangeDateTimeFilter,MultipleChoicesDropdownFilter
from django.urls import path
from configurator.views import ProductSelectionView, ConfigurationWizardView, ConfigurationExportView
from django.urls import reverse
from django.shortcuts import redirect
from unfold.decorators import action
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html, format_html_join
from django.http import HttpRequest
from invoices.models import AppliedTax
from unfold.decorators import display
from core.admin.company_admin import CompanyRestrictedAdmin
from configurator.views.configuration.autocomplete_views import ClientAutocompleteView, AssetAutocompleteView, QuoteAutocompleteView, ProjectFileAutocompleteView
from configurator.services.receival_slip_service import ReceivalSlipService
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import ExportForm, ImportForm, SelectableFieldsExportForm
from django.http import HttpResponse
from configurator.resources.configuration_resource import SummarizedConfigurationResource, DetailedItemsResource



class ConfigurationComponentGroupInline(StackedInline):
    verbose_name = _("Component group")
    verbose_name_plural = _("Component groups")
    hide_ordering_field = True
    ordering_field = "order"
    model = ConfigurationComponentGroup
    extra = 0
    tab=True

    readonly_fields = (
        "material_total",
        "total",
    )

    fields = (
        "order",
        "key",
        "label",
        "items",
        "material_total",
        "total",
    )

    filter_horizontal = ("items",)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        """
        Limit selectable ConfigurationItems to those
        belonging to the current Configuration.
        """
        if db_field.name == "items":
            object_id = request.resolver_match.kwargs.get("object_id")
            if object_id:
                kwargs["queryset"] = ConfigurationItem.objects.filter(
                    configuration_id=object_id
                )
            else:
                kwargs["queryset"] = ConfigurationItem.objects.none()

        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def has_change_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return False
        return super().has_change_permission(request, obj)

    def has_add_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return False
        return super().has_delete_permission(request, obj)



class AppliedTaxInline(GenericTabularInline):
    hide_ordering_field = True
    ordering_field = "order"
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)

    fieldset = {
        "Applied tax": (("tax_type", "selected_percentage"),("base_amount","calculated_amount"), "order")
    }
    verbose_name = _("Applied tax")
    verbose_name_plural = _("Applied taxes")
    tab = True
    def has_change_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return False
        return super().has_change_permission(request, obj)
    
    def has_add_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return False
        return super().has_delete_permission(request, obj)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tax_type")

class ParameterValueInline(TabularInline):
    hide_ordering_field = True
    ordering_field = "order"
    model = ConfigurationParameterValue
    extra = 0
    fields = ("parameter", "value","order")
    tab = True
    readonly_fields = ["parameter"]
    verbose_name = _("Parameter value")
    verbose_name_plural = _("Parameter values")

    def _can_edit_parameter_values(self, obj):
        if not obj:
            return True
        if obj.status in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return True
        return bool(
            getattr(obj.product, "allow_parameter_values_edit_in_any_status", False)
        )
    
    def has_change_permission(self, request, obj=None):
        if not self._can_edit_parameter_values(obj):
            return False
        return super().has_change_permission(request, obj)
    
    def has_add_permission(self, request, obj=None):
        if not self._can_edit_parameter_values(obj):
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if not self._can_edit_parameter_values(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parameter")
class ConfigurationItemInline(StackedInline):
    model = ConfigurationItem
    extra = 0
    readonly_fields = ("line_total", "is_template_item")
    tab = True
    autocomplete_fields = ["component"]
    verbose_name = _("Configuration item")
    verbose_name_plural = _("Configuration items")
    fields = (
        ("component",
        "quantity",
        "length",),
        ("unit_price",
        "line_total",),
        "note",
    )

    def has_change_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return False
        return super().has_change_permission(request, obj)
    
    def has_add_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE]:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE] and obj.status == ConfigurationStatus.COMPLETED:
            return False
        return super().has_delete_permission(request, obj)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("component")

class PricingRuleAssignmentInline(TabularInline):
    model = PricingRuleAssignment
    extra = 0
    autocomplete_fields = ["rule"]
    tab = True
    verbose_name = _("Pricing rule assignment")
    verbose_name_plural = _("Pricing rule assignments")
    fields = ("rule", "enabled", "evaluated_amount", "evaluation_status")
    readonly_fields = ("evaluated_amount", "evaluation_status")

    @staticmethod
    def _rule_result(obj):
        """Result recorded for this rule in the configuration's last pricing run."""
        if obj is None or obj.pk is None or obj.configuration_id is None:
            return None
        breakdown = obj.configuration.pricing_breakdown or {}
        if not isinstance(breakdown, dict):
            return None
        for result in breakdown.get("rules", []):
            if result.get("rule_id") == obj.rule_id:
                return result
        return None

    @admin.display(description=_("Amount"))
    def evaluated_amount(self, obj):
        result = self._rule_result(obj)
        if result is None:
            return "—"
        return f"{result['amount']} {obj.configuration.subtotal_price.currency}"

    @admin.display(description=_("Result"))
    def evaluation_status(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        config = obj.configuration
        if config.price_override is not None and config.price_override.amount >= 0:
            return _("Not evaluated (price override)")
        if not obj.enabled:
            return _("Disabled")
        result = self._rule_result(obj)
        if result is None:
            return _("Not calculated yet")
        if not result.get("applied"):
            return _("No effect (amount is 0)")
        label = _("Charge") if result.get("type") == "charge" else _("Discount")
        if result.get("type") == "discount" and not result.get("show_in_export", True):
            label = _("Discount (hidden in export)")
        return f"{label} → {result.get('subtotal_after', '')}"

    def has_change_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE] and obj.status == ConfigurationStatus.COMPLETED:
            return False
        return super().has_change_permission(request, obj)
    
    def has_add_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE] and obj.status == ConfigurationStatus.COMPLETED:
            return False
        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE] and obj.status == ConfigurationStatus.COMPLETED:
            return False
        return super().has_delete_permission(request, obj)

@admin.register(Configuration)
class ConfigurationAdmin(ImportExportModelAdmin, CompanyRestrictedAdmin, ModelAdmin):
    resource_classes = [SummarizedConfigurationResource, DetailedItemsResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm
    custom_urls = (
        (
            "autocomplete/client/",
            "client_autocomplete",
            ClientAutocompleteView.as_view(),
        ),
        (
            "autocomplete/asset/",
            "asset_autocomplete",
            AssetAutocompleteView.as_view(),
        ),
        (
            "autocomplete/quote/",
            "quote_autocomplete",
            QuoteAutocompleteView.as_view(),
        ),
        (
            "autocomplete/project/",
            "project_autocomplete",
            ProjectFileAutocompleteView.as_view(),
        ),
    )
    list_display = (
        "id",
        "name",
        "product__name",
        "client",
        "quantity",
        "subtotal_price",
        "total_price_display",
        "show_status_customized_color",
        "show_planning_status_badge",
    )

    list_filter = (("company",AutocompleteSelectMultipleFilter),
                    ("status",MultipleChoicesDropdownFilter),
                    ("planning_status",MultipleChoicesDropdownFilter),
                   ("client",AutocompleteSelectMultipleFilter), 
                   ("product",AutocompleteSelectMultipleFilter),
                   ("created_at",RangeDateTimeFilter))
    search_fields = ("name", "client__name")
    list_select_related = ["client","product"]

    readonly_fields = ("total_price","total_cost","subtotal_price","company","planning_status","planned_at","computed_variables_display")
    actions_row = ["export_configuration_to_template",
                   "duplicate_configuration",
                   "convert_to_production",
                   "record_actual_values",
                   "resync_planning_from_template",
                   "create_receival_slip_action",
                   "view_receival_slips"]
    actions_detail = ["convert_to_production_detail_action"]
    autocomplete_fields = ["client","product","asset","quote","project_file"]
    list_filter_submit = True

    inlines = [
        ParameterValueInline,
        ConfigurationItemInline,
        PricingRuleAssignmentInline,
        AppliedTaxInline,
        ConfigurationComponentGroupInline,
    ]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            ConfigurationStatus.COMPLETED: "success",  # green
            ConfigurationStatus.DRAFT: "info",  # blue
            ConfigurationStatus.CONVERTED: "warning",  # orange
            ConfigurationStatus.PRODUCTION_DONE: "success", # purple/grey
            ConfigurationStatus.SHOP: "warning", # purple/grey
        },
    )
    def show_status_customized_color(self, obj):
        return obj.status, obj.get_status_display()

    @display(
        description=_("Planning"),
        ordering="planning_status",
        label={
            ConfigurationPlanningStatus.PLANNED: "success",
            ConfigurationPlanningStatus.UNPLANNED: "warning",
        },
    )
    def show_planning_status_badge(self, obj):
        return obj.planning_status, obj.get_planning_status_display()

    fieldsets = (
        (_("Configuration"), {
            "fields": (
                ("quote"),
                ("name", "client"),
                ("product", "status"),
                ("planning_status", "planned_at"),
                ("company", "asset"),
                ("project_file",),
            ),
        }),
        (_("Pricing"), {
            "fields": (
                ("total_cost", "subtotal_price"),
                ("price_override", "total_price"),
                ("quantity", "unit_price"),
                ),
        }),
        (_("Computed variables"), {
            "fields": ("computed_variables_display",),
            "classes": ("collapse",),
            "description": _("Values of the product's computed variables from the last price calculation."),
        }),
        (_("Notes"), {
            "fields": (
                ("notes",),
            ),
        }),
    )

    @admin.display(description=_("Computed variables"))
    def computed_variables_display(self, obj):
        values = (obj.computed_variables if obj else None) or {}
        if not values:
            return _("Not calculated yet")
        return format_html(
            '<table class="w-full text-sm"><tbody>{}</tbody></table>',
            format_html_join(
                "",
                '<tr class="border-b border-base-200 last:border-0 dark:border-base-800">'
                '<td class="py-1 pr-4 align-top whitespace-nowrap">'
                '<code class="font-mono text-xs">{}</code></td>'
                '<td class="py-1 align-top">{}</td></tr>',
                sorted(values.items()),
            ),
        )

    def get_readonly_fields(self, request, obj=None):
        readonly = super().get_readonly_fields(request, obj)
        if obj and obj.status != ConfigurationStatus.DRAFT:
            all_fields = []
            for name, options in self.fieldsets:
                for field in options['fields']:
                    if isinstance(field, (list, tuple)):
                        all_fields.extend(field)
                    else:
                        all_fields.append(field)
            
            excluded_from_readonly = {"quote","project_file", "client", "notes","name"}
            if obj.status == ConfigurationStatus.SHOP:
                excluded_from_readonly.add("price_override")
            all_fields = [f for f in all_fields if f not in excluded_from_readonly]
            
            if obj.status == ConfigurationStatus.PRODUCTION_DONE:
                 return list(readonly)
            
            return list(set(list(readonly) + all_fields))
        return readonly

    def get_wizard_view(self, product_id):
        from configurator.models import ProductTemplate, ProductFlowKind
        from configurator.views.configuration.shop_wizard_views import ShopConfigurationWizardView
        from configurator.views import ConfigurationWizardView
        
        product = ProductTemplate.objects.get(pk=product_id)
        if product.flow_kind == ProductFlowKind.SHOP:
            return ShopConfigurationWizardView
        return ConfigurationWizardView

    def get_wizard_view_for_config(self, config_id):
        from configurator.models import Configuration, ProductFlowKind
        from configurator.views.configuration.shop_wizard_views import ShopConfigurationWizardView
        from configurator.views import ConfigurationWizardView
        
        config = Configuration.objects.get(pk=config_id)
        if config.product.flow_kind == ProductFlowKind.SHOP:
            return ShopConfigurationWizardView
        return ConfigurationWizardView

    
    def get_urls(self):

        urls = super().get_urls()
        custom_urls = [
            path(
                "wizard/",
                self.admin_site.admin_view(ProductSelectionView.as_view(model_admin=self)),
                name="configurator_configuration_wizard_select",
            ),
            path(
                "wizard/<int:product_id>/",
                self.admin_site.admin_view(
                    lambda request, product_id: self.get_wizard_view(product_id).as_view(model_admin=self)(request, product_id=product_id)
                ),
                name="configurator_configuration_wizard_form",
            ),
            path(
                "wizard/edit/<int:product_id>/<int:config_id>/",
                self.admin_site.admin_view(
                    lambda request, product_id, config_id: self.get_wizard_view_for_config(config_id).as_view(model_admin=self)(request, product_id=product_id, config_id=config_id)
                ),
                name="configurator_configuration_wizard_edit",
            ),
            path(
            "<int:config_id>/export/",
            self.admin_site.admin_view(
                ConfigurationExportView.as_view(model_admin=self)
            ),
            name="configurator_configuration_export",
        ),
        ]
        return custom_urls + urls

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.generate_bom()
        form.instance.recalculate_price()

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        if formset.model is ConfigurationComponentGroup:
            form.instance.recompute_component_groups()


    def add_view(self, request, form_url="", extra_context=None):
        """
        Redirect the default 'Add' button to the wizard.
        """
        return redirect(
            reverse("admin:configurator_configuration_wizard_select")
        )

    @action(
        description=_("Export HTML"),
        url_path="configurationexportadmin-export-action",
        attrs={"target": "_blank"}
    )
    def export_configuration_to_template(self, request: HttpRequest, object_id: int):
        url = reverse("admin:configurator_configuration_export", args=[object_id])
        return redirect(url)

    @action(
        description=_("Duplicate"),
        url_path="configurationadmin-duplicate",
    )
    def duplicate_configuration(self, request: HttpRequest, object_id: int):
        """
        Clone this configuration and redirect to the new copy's edit page.
        """
        obj = Configuration.objects.get(pk=object_id)
        new_config = obj.clone()

        self.message_user(
            request,
            _("Configuration '%(name)s' was successfully duplicated.")
            % {"name": obj.name},
            "success",
        )

        return redirect(
            reverse("admin:configurator_configuration_change", args=[new_config.pk])
        )

    @admin.display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price
    
    def _convert_to_production(self, request, config):
        if (
            config.quote
            and config.quote.status != QuoteStatus.ACCEPTED
            and not config.product.allow_production_without_accepted_quote
        ):
            self.message_user(
                request,
                _("Cannot convert to production. The associated quote must be accepted."),
                messages.ERROR
            )
            return None

        flow = ConfigurationFlow(config)
        ticket = flow.convert()
        self.message_user(
                request,
                _("Configuration successfully converted to production."),
                "success")
        return ticket

    @action(
    description=_("Convert to production"),
    attrs={"target": "_blank"}

)
    def convert_to_production(self, request: HttpRequest, object_id: int):
        config = Configuration.objects.get(pk=object_id)
        ticket = self._convert_to_production(request, config)
        if ticket is None:
            return redirect(request.META.get('HTTP_REFERER', 'admin:configurator_configuration_changelist'))

        return redirect(
            reverse("admin:production_productionticket_change", args=[ticket.id])
        )

    @action(
        description=_("Convert to production"),
        attrs={"target": "_blank"},
        permissions=["config_quote_is_accepted"],
    )
    def convert_to_production_detail_action(self, request: HttpRequest, object_id: int):
        config = Configuration.objects.get(pk=object_id)
        ticket = self._convert_to_production(request, config)
        if ticket is None:
            return redirect(reverse("admin:configurator_configuration_change", args=[object_id]))

        return redirect(
            reverse("admin:production_productionticket_change", args=[ticket.id])
        )

    def has_config_quote_is_accepted_permission(self, request, object_id=None):
        if not object_id:
            return True
        config = Configuration.objects.filter(pk=object_id).select_related("quote", "product").first()
        if not config:
            return False
        return (
            not config.quote
            or config.quote.status == QuoteStatus.ACCEPTED
            or config.product.allow_production_without_accepted_quote
        )

    @action(description=_("Create Receival Slip"))
    def create_receival_slip_action(self, request: HttpRequest, object_id: int):
        config = Configuration.objects.get(pk=object_id)
        slip = ReceivalSlipService.create_from_configuration(config)
        
        self.message_user(
            request,
            _("Receival Slip successfully created."),
            "success"
        )

        return redirect(
            reverse("admin:configurator_receivalslip_change", args=[slip.id])
        )

    @action(description=_("View Receival Slips"))
    def view_receival_slips(self, request: HttpRequest, object_id: int):
        url = reverse("admin:configurator_receivalslip_changelist")
        return redirect(f"{url}?configuration__id__exact={object_id}")

    @action(
        description=_("Record Actual Values"),
        url_path="record-actual-values",
    )
    def record_actual_values(self, request, object_id: int):
        config = Configuration.objects.get(pk=object_id)
        if config.status not in [ConfigurationStatus.SHOP,ConfigurationStatus.DRAFT]:
            self.message_user(
                request,
                _("Cannot record actual values. The configuration must be in production."),
                messages.ERROR
            )
            return redirect(request.META.get('HTTP_REFERER', 'admin:configurator_configuration_changelist'))
             
        url = reverse("admin:configurator_configuration_wizard_edit", args=[config.product_id,object_id])
        return redirect(url)

    @action(
        description=_("Resync Planning"),
        permissions=["resync_planning"],
        url_path="resync-planning",
    )
    def resync_planning_from_template(self, request: HttpRequest, object_id: int):
        config = Configuration.objects.select_related("product").get(pk=object_id)
        updated = config.resync_planning_from_template()

        if updated:
            self.message_user(
                request,
                _("Planning configuration resynced from template. %(count)d item(s) updated.")
                % {"count": updated},
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                _("Planning configuration is already up to date."),
                messages.INFO,
            )

        return redirect(
            request.META.get(
                "HTTP_REFERER",
                reverse("admin:configurator_configuration_change", args=[object_id]),
            )
        )

    def has_resync_planning_permission(self, request: HttpRequest):
        return request.user.is_superuser
