from decimal import Decimal

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import display
from .models import (
    Client, Order, DeliveryOrder, DeliveryOrderItem, DeliveryOrderStatus,
    Quote, QuoteItem, QuoteInventoryItem, SaleZone, ClientAddress, ClientContact, ClientBankAccount,
    QuoteStatus, QuoteConfigurationGroup, QuotePhoto, QuotePhotoCategory,
)
from unfold.admin import TabularInline, StackedInline
from import_export.admin import ImportExportModelAdmin
from .forms import (
    ClientImportForm, ClientExportForm,
    QuoteImportForm, ClientConfirmImportForm, QuoteConfirmImportForm,
    QuoteItemImportForm, QuoteItemConfirmImportForm,
)
from invoices.models import AppliedTax

from configurator.models import Configuration
from configurator.admin.configuration import AppliedTaxInline
from django.shortcuts import redirect, render
from django.urls import reverse, path
from unfold.decorators import action
from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.company_access import get_user_companies
from .resources import ClientResource, QuoteImportResource, QuoteItemImportResource
from .views_analysis import ConversionAnalysisView
from management.views import QuoteAcceptanceWizardView
from .views.quote_export_view import QuoteExportView
from .views.quote_dashboard_view import QuoteDashboardView
from .components.quote_dashboard_charts import QuoteValueByStatusChart, QuoteCountByStatusChart  # noqa: F401
from core.tasks import send_quote_email_task
from company.models import EmailLog
from unfold.admin import GenericTabularInline as _GenericTabularInline
from configurator.commission.admin import QuoteCommissionInline
from configurator.commission.services import CommissionService



# ─────────────────────────────────────────────────────────────────────────────
# Mixin — shared company-passthrough logic for all import admins
# ─────────────────────────────────────────────────────────────────────────────

class _CompanyImportMixin:

    def get_import_form_kwargs(self, request):
        kwargs = super().get_import_form_kwargs(request)
        kwargs["companies"] = get_user_companies(request.user)
        kwargs["request"] = request
        return kwargs

    def get_confirm_form_initial(self, request, import_form):
        initial = super().get_confirm_form_initial(request, import_form)
        if import_form and hasattr(import_form, "cleaned_data"):
            company = import_form.cleaned_data.get("company")
            if company:
                initial["company"] = str(company.pk)
        return initial

    def get_import_resource_kwargs(self, request, **kwargs):
        from company.models import Company as CompanyModel
        result = super().get_import_resource_kwargs(request, **kwargs)
        form = kwargs.get("form")
        company = None
        if form and hasattr(form, "cleaned_data"):
            raw = form.cleaned_data.get("company")
            if raw:
                if hasattr(raw, "pk"):
                    company = raw
                else:
                    try:
                        company = CompanyModel.objects.get(pk=raw)
                    except CompanyModel.DoesNotExist:
                        pass
        if company:
            result["default_company"] = company
        else:
            companies = get_user_companies(request.user)
            if companies.count() == 1:
                result["default_company"] = companies.first()
        return result

@admin.register(SaleZone)
class SaleZoneAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "company"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]

@admin.register(ClientBankAccount)
class ClientBankAccountAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ["bank_name", "account_number", "client"]
    search_fields = ["bank_name", "account_number", "iban"]
    autocomplete_fields = ["client"]

@admin.register(ClientAddress)
class ClientAddressAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ["street", "city", "province", "country", "client"]
    search_fields = ["street", "city", "postal_code", "province", "country", "client__name"]
    autocomplete_fields = ["client"]

class ClientAddressInline(StackedInline):
    model = ClientAddress
    extra = 0
    tab = True
    tab_title = _("Addresses")
    fieldsets = (
        (None, {
            "fields": (
                "address_type",
                "country",
                ("province", "city", "postal_code"),
                "street",
            ),
        }),
    )

class ClientContactInline(StackedInline):
    model = ClientContact
    extra = 0
    tab = True
    tab_title = _("Contacts")
    autocomplete_fields = ["address"]
    fieldsets = (
        (None, {
            "fields": (
                ("name", "position"),
                ("email", "phone"),
                "address",
            ),
        }),
    )

class ClientBankAccountInline(StackedInline):
    model = ClientBankAccount
    extra = 0
    tab = True
    tab_title = _("Bank Accounts")
    fieldsets = (
        (None, {
            "fields": (
                ("bank_name", "holder_name"),
                ("iban", "swift_bic"),
                ("account_number", "routing_number"),
                ("country", "is_default"),
            ),
        }),
    )

@admin.register(Client)
class ClientAdmin(_CompanyImportMixin, CompanyRestrictedAdmin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [ClientResource]
    import_form_class = ClientImportForm
    export_form_class = ClientExportForm

    list_display = ["name", "tax_identifier", "phone", "address_truncated", "company",  "has_signed_terms"]
    search_fields = ["name", "tax_identifier", "email", "phone", "address"]
    list_filter = ["sale_zone", "is_insured", "company"]

    def get_list_filter(self, request):
        return self.list_filter

    def get_confirm_form_class(self, request):
        return ClientConfirmImportForm

    @display(description=_("Address"))
    def address_truncated(self, obj):
        if not obj.address or obj.address == "-":
            return "-"
        return obj.address if len(obj.address) <= 40 else obj.address[:40] + "…"

    autocomplete_fields = ["company", "agent", "sale_zone", "our_bank_account"]

    inlines = [ClientAddressInline, ClientContactInline, ClientBankAccountInline]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "default_client_bank_account":
            import re
            match = re.search(r'/admin/sales/client/(\d+)/change/', request.path)
            if match:
                client_id = match.group(1)
                kwargs["queryset"] = ClientBankAccount.objects.filter(client_id=client_id)
            else:
                kwargs["queryset"] = ClientBankAccount.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @display(
        description=_("T&C firmados"),
        label={
            _("Sí"): "success",
            _("No"): "danger",
        },
    )
    def has_signed_terms(self, obj):
        return _("Sí") if obj.terms_signature else _("No")

    @display(description=_("Firma"))
    def terms_signature_preview(self, obj):
        if obj.terms_signature:
            return format_html(
                '<img src="{}" style="width:400px;height:150px;object-fit:contain;border:1px solid #ccc;background:white;" />',
                obj.terms_signature.url,
            )
        return _("Sin firma")

    fieldsets = (
        (_("Basic Information"), {
            "classes": ["tab"],
            "fields": (
                ("name", "company"),
                ("tax_identifier", "phone", "email"),
                "address"
            )
        }),
        (_("Sales & Territory"), {
            "classes": ["tab"],
            "fields": (
                ("sale_zone", "agent"),
                "agent_commission",
            )
        }),
        (_("Financial & Insurance"), {
            "classes": ["tab"],
            "fields": (
                ("is_insured", "max_risk", "current_risk"),
                ("payment_method", "our_bank_account"),
                "default_client_bank_account",
            )
        }),
        (_("Términos y Condiciones"), {
            "classes": ["tab"],
            "fields": (
                "has_signed_terms",
                "terms_accepted_at",
                "accepted_terms_company",
                "terms_version_display",
                "terms_signature_preview",
            )
        }),
    )
    readonly_fields = [
        "has_signed_terms",
        "terms_accepted_at",
        "accepted_terms_company",
        "terms_version_display",
        "terms_signature_preview",
    ]

    @display(description=_("Versión T&C"))
    def terms_version_display(self, obj):
        return obj.terms_version or "—"

@admin.register(Order)
class OrderAdmin(ModelAdmin):
    list_display = [
        "project_name",
        "client",
        "status",
        "deadline_date",
        "final_price_display"
    ]
    list_filter = ["status", "date_created", "deadline_date"]
    search_fields = ["project_name", "client__name"]
    autocomplete_fields = ["client"]
    readonly_fields = ["date_created", "final_price_display","total_cost_material","total_cost_labor"]
    fieldsets = (
        ("Project Details", {
            "fields": (
                ("project_name", "status"),
                "client",
                ("date_created", "deadline_date")
            )
        }),
        ("Financial Summary", {
            "classes": ("collapse",),
            "description": "Calculated costs based on the manufacturing items associated with this order.",
            "fields": (
                ("total_cost_material", "total_cost_labor"),
                "final_price"
            )
        }),
    )

    def final_price_display(self, instance):
        return f"{instance.final_price:.2f} €"
    final_price_display.short_description = "Total Price"
    final_price_display.admin_order_field = "final_price"


class DeliveryOrderItemInline(TabularInline):
    model = DeliveryOrderItem
    extra = 0
    tab = True

@admin.register(DeliveryOrder)
class DeliveryOrderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "incremental_id", "get_status_label", "company", "client", "date", "sales_invoice"]
    list_filter = ["company", "status", "date"]
    search_fields = ["number", "client__name"]
    autocomplete_fields = ["company", "client", "sales_invoice", "configuration", "project_file"]
    inlines = [DeliveryOrderItemInline]
    readonly_fields = ["number", "date"]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(DeliveryOrderStatus.DRAFT): "warning",
            _(DeliveryOrderStatus.DELIVERED): "success",
            _(DeliveryOrderStatus.SENT): "info",
            _(DeliveryOrderStatus.CANCELLED): "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status

class QuotePhotoInline(TabularInline):
    model = QuotePhoto
    extra = 0
    tab = True
    tab_title = _("Photos")
    fields = ["image", "category", "caption", "uploaded_at"]
    readonly_fields = ["uploaded_at"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "category":
            import re
            match = re.search(r'/admin/sales/quote/(\d+)/change/', request.path)
            if match:
                quote = Quote.objects.select_related("company").get(pk=match.group(1))
                kwargs["queryset"] = QuotePhotoCategory.objects.filter(
                    company=quote.company, is_active=True
                ).order_by("order")
            else:
                kwargs["queryset"] = QuotePhotoCategory.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(QuotePhotoCategory)
class QuotePhotoCategoryAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "order", "is_active", "company"]
    list_filter = ["company", "is_active"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]
    list_editable = ["order", "is_active"]


class QuoteItemInline(TabularInline):
    model = QuoteItem
    extra = 1
    tab = True
    autocomplete_fields = ["material"]
    readonly_fields = ["line_total", "cost_display", "margin_display"]
    fields = ["material", "quantity", "unit_price", "discount", "line_total", "cost_display", "margin_display"]

    @display(description=_("Coste"))
    def cost_display(self, obj):
        if not obj.pk or not obj.material_id:
            return "—"
        cost = obj.material.cost_per_unit
        if not cost or cost.amount == 0:
            return "—"
        return cost

    @display(description=_("Margen"))
    def margin_display(self, obj):
        if not obj.pk or not obj.material_id:
            return "—"
        cost = obj.material.cost_per_unit
        if not cost or cost.amount == 0 or not obj.unit_price or obj.unit_price.amount == 0:
            return "—"
        disc = (Decimal("100") - obj.discount) / Decimal("100")
        sale = obj.unit_price.amount * disc
        if sale == 0:
            return "—"
        margin = (sale - cost.amount) / sale * 100
        color = "#22c55e" if margin >= 0 else "#ef4444"
        margin_str = f"{float(margin):.1f}%"
        return format_html('<span style="color:{};font-weight:600;">{}</span>', color, margin_str)


class QuoteInventoryItemInline(TabularInline):
    model = QuoteInventoryItem
    extra = 1
    tab = True
    autocomplete_fields = ["inventory_item"]
    readonly_fields = ["line_total", "total_with_tax"]
    fields = ["inventory_item", "quantity", "unit_price", "discount", "line_total", "total_with_tax"]
    verbose_name = _("Sale item")
    verbose_name_plural = _("Sale items")

class QuoteConfigurationGroupInline(StackedInline):
    model = QuoteConfigurationGroup
    extra = 1
    tab = True
    ordering_field= "order"
    hide_ordering_field=True
    tab_title = _("Configuration Groups")
    fields = ["name", "notes", "order"]
    ordering = ["order"]


@admin.register(QuoteConfigurationGroup)
class QuoteConfigurationGroupAdmin(ModelAdmin):
    list_display = ["name", "quote", "order"]
    search_fields = ["name"]
    autocomplete_fields = ["quote"]
    list_filter = ["quote"]


class ConfigurationReadOnlyInline(TabularInline):
    model = Configuration
    extra = 0
    tab = True
    readonly_fields = ["config_link", "status", "subtotal_price", "total_price_display"]
    fields = ["config_link", "status", "quote_group", "subtotal_price", "total_price_display"]
    can_delete = True
    max_num = 0

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "quote_group":
            import re
            match = re.search(r'/admin/sales/quote/(\d+)/change/', request.path)
            if match:
                kwargs["queryset"] = QuoteConfigurationGroup.objects.filter(
                    quote_id=match.group(1)
                ).order_by("order")
            else:
                kwargs["queryset"] = QuoteConfigurationGroup.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @display(description=_("Name"))
    def config_link(self, obj):
        from django.utils.html import format_html
        url = reverse("admin:configurator_configuration_change", args=[obj.pk])
        return format_html('<a href="{}" target="_blank">{}</a>', url, obj.name)

    @display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price


class QuoteEmailLogInline(_GenericTabularInline):
    model = EmailLog
    extra = 0
    tab = True
    verbose_name = _("Email Sent")
    verbose_name_plural = _("Emails Sent")
    fields = ("to_email", "subject", "sent_at", "status", "error_message")
    readonly_fields = ("to_email", "subject", "sent_at", "status", "error_message")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

@admin.register(Quote)
class QuoteAdmin(_CompanyImportMixin, CompanyRestrictedAdmin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [QuoteImportResource]
    import_form_class = QuoteImportForm
    export_form_class = ClientExportForm
    list_display = ["id", "project_name", "client", "date", "project_file", "show_status_customized_color", "total_price_display", "company"]
    list_filter = ["status", "company", "created_at"]
    search_fields = ["project_name", "client__name", "id"]
    autocomplete_fields = ["client", "project_file", "company"]
    readonly_fields = ["subtotal_price", "tax_amount", "total_price", "tax_breakdown", "date_sent", "date_accepted", "date_rejected"]
    list_select_related = ["client", "project_file__client", "company"]
    
    inlines = [ConfigurationReadOnlyInline, 
    QuoteConfigurationGroupInline, 
    QuoteInventoryItemInline, 
    QuotePhotoInline, 
    AppliedTaxInline, 
    QuoteEmailLogInline,
    QuoteCommissionInline]


    actions_list = [ "quote_dashboard_action"]
    actions_row = ["add_configuration_to_quote", "export_quote_action", "send_email_row_action"]
    actions_detail = ["send_email_detail_action"]

    fieldsets = (
        ("Quote Details", {
            "fields": (
                ("client", "status"),
                ("project_name", "company"),
                ("date", "expires_at"),
                "project_file",
            )
        }),
        (_("Status Dates"), {
            "fields": (
                ("date_sent", "date_accepted", "date_rejected"),
            ),
            "classes": ("collapse",),
        }),
        ("Financials", {
            "fields": (
                ("subtotal_price", "tax_amount"),
                "total_price",
                "tax_breakdown"
            )
        }),
    )

    def get_confirm_form_class(self, request):
        return QuoteConfirmImportForm

    @display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(QuoteStatus.ACCEPTED.label): "success",
            _(QuoteStatus.SENT.label): "info",
            _(QuoteStatus.REJECTED.label): "warning",
            _(QuoteStatus.DRAFT.label): "primary",
        },
    )
    def show_status_customized_color(self, obj):
        return _(obj.get_status_display())

    @action(description=_("Add Configuration"))
    def add_configuration_to_quote(self, request, object_id: int):
        url = reverse("admin:configurator_configuration_wizard_select")
        return redirect(f"{url}?quote_id={object_id}")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalc_totals()
        # Pre-populate and recalculate only on save
        CommissionService.prepopulate_for_quote(form.instance)
        CommissionService.recalculate(form.instance)

    @action(description=_("Export Quote"), attrs={"target": "_blank"})
    def export_quote_action(self, request, object_id: int):
        return redirect(reverse("admin:sales_quote_export", args=[object_id]))

    @action(
        description=_("Quote Dashboard"),
        url_path="quote-dashboard-action",
        permissions=["quote_dashboard"],
        icon="bar_chart",
    )
    def quote_dashboard_action(self, request):
        return redirect(reverse("admin:sales_quote_dashboard"))

    def has_quote_dashboard_permission(self, request):
        return request.user.has_perm("sales.can_view_quote_dashboard")

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "conversion-analysis/",
                self.admin_site.admin_view(
                    ConversionAnalysisView.as_view(model_admin=self)
                ),
                name="sales_quote_conversion_analysis",
            ),
            path(
                "dashboard/",
                self.admin_site.admin_view(
                    QuoteDashboardView.as_view(model_admin=self)
                ),
                name="sales_quote_dashboard",
            ),
            path(
                "<int:quote_id>/accept-wizard/",
                self.admin_site.admin_view(QuoteAcceptanceWizardView.as_view(model_admin=self)),
                name="sales_quote_acceptance_wizard",
            ),
            path(
                "<int:quote_id>/export/",
                self.admin_site.admin_view(QuoteExportView.as_view(model_admin=self)),
                name="sales_quote_export",
            ),
        ]
        return custom + urls

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalc_totals()

    @action(description=_("Export Quote"), attrs={"target": "_blank"})
    def export_quote_action(self, request, object_id: int):
        return redirect(reverse("admin:sales_quote_export", args=[object_id]))

    @action(
        description=_("Quote Dashboard"),
        url_path="quote-dashboard-action",
        permissions=["quote_dashboard"],
        icon="bar_chart",
    )
    def quote_dashboard_action(self, request):
        return redirect(reverse("admin:sales_quote_dashboard"))

    def has_quote_dashboard_permission(self, request):
        return request.user.has_perm("sales.can_view_quote_dashboard")

    def response_change(self, request, obj):
        if obj.status == "accepted" and not obj.project_file:
            return redirect("admin:sales_quote_acceptance_wizard", quote_id=obj.pk)
        return super().response_change(request, obj)

    def response_add(self, request, obj, post_url_continue=None):
        if obj.status == "accepted" and not obj.project_file:
            return redirect("admin:sales_quote_acceptance_wizard", quote_id=obj.pk)
        return super().response_add(request, obj, post_url_continue)

    @action(description=_("Send Email"), icon="mail")
    def send_email_row_action(self, request, object_id):
        quote = Quote.objects.select_related("client").get(pk=object_id)
        if not quote.client or not quote.client.email:
            self.message_user(request, _("Client has no email address."), level="error")
        else:
            send_quote_email_task.delay(quote.id)
            self.message_user(request, _("Quote email queued for %s.") % quote.client.email)
        return redirect(reverse("admin:sales_quote_changelist"))

    @action(description=_("Send Email"), icon="mail")
    def send_email_detail_action(self, request, object_id):
        quote = Quote.objects.select_related("client").get(pk=object_id)
        if not quote.client or not quote.client.email:
            self.message_user(request, _("Client has no email address."), level="error")
        else:
            send_quote_email_task.delay(quote.id)
            self.message_user(request, _("Quote email queued for %s.") % quote.client.email)
        return redirect(reverse("admin:sales_quote_change", args=[object_id]))


@admin.register(QuoteItem)
class QuoteItemAdmin(_CompanyImportMixin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [QuoteItemImportResource]
    import_form_class = QuoteItemImportForm

    list_display = ["quote_name", "material_name", "unit_price", "discount", "line_total", "cost_display", "margin_display"]
    search_fields = ["quote__project_name", "material__reference_code", "material__name"]
    autocomplete_fields = ["quote", "material", "category"]
    readonly_fields = ["line_total", "cost_display"]
    list_select_related = ["quote", "material"]

    def get_confirm_form_class(self, request):
        return QuoteItemConfirmImportForm

    @display(description=_("Quote"))
    def quote_name(self, obj):
        return obj.quote.project_name if obj.quote_id else "-"

    @display(description=_("Material"))
    def material_name(self, obj):
        return obj.material.name if obj.material_id else "-"

    @display(description=_("Coste"))
    def cost_display(self, obj):
        if not obj.material_id:
            return "—"
        cost = obj.material.cost_per_unit
        if not cost or cost.amount == 0:
            return "—"
        return cost

    @display(description=_("Margen"))
    def margin_display(self, obj):
        if not obj.material_id:
            return "—"
        cost = obj.material.cost_per_unit
        if not cost or cost.amount == 0 or not obj.unit_price or obj.unit_price.amount == 0:
            return "—"
        disc = (Decimal("100") - obj.discount) / Decimal("100")
        sale = obj.unit_price.amount * disc
        if sale == 0:
            return "—"
        margin = (sale - cost.amount) / sale * 100
        color = "#22c55e" if margin >= 0 else "#ef4444"
        margin_str = f"{float(margin):.1f}%"
        return format_html('<span style="color:{};font-weight:600;">{}</span>', color, margin_str)


class QuoteInventoryItemAppliedTaxInline(_GenericTabularInline):
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    verbose_name = _("Applied tax")
    verbose_name_plural = _("Applied taxes")
    tab = True

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tax_type")


@admin.register(QuoteInventoryItem)
class QuoteInventoryItemAdmin(ModelAdmin):
    list_display = ["quote_name", "inventory_item_name", "unit_price", "discount", "line_total"]
    search_fields = ["quote__project_name", "inventory_item__code", "inventory_item__name"]
    autocomplete_fields = ["quote", "inventory_item"]
    readonly_fields = ["line_total"]
    list_select_related = ["quote", "inventory_item"]
    inlines = [QuoteInventoryItemAppliedTaxInline]

    @display(description=_("Quote"))
    def quote_name(self, obj):
        return obj.quote.project_name if obj.quote_id else "-"

    @display(description=_("Inventory Item"))
    def inventory_item_name(self, obj):
        return str(obj.inventory_item) if obj.inventory_item_id else "-"
