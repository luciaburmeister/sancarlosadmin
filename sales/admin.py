from unfold.contrib.filters.admin import RangeDateFilter
from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from decimal import Decimal
from djmoney.money import Money

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import display
from .models import (
    Client, Order, DeliveryOrder, DeliveryOrderItem, DeliveryOrderStatus,
    Quote, QuoteItem, QuoteInventoryItem, SaleZone, ClientAddress, ClientContact, ClientBankAccount,
    QuoteStatus, QuoteConfigurationGroup, QuotePhoto, QuotePhotoCategory,
    DiscountPolicy, DiscountRule, CommissionistAgent,
)
from inventory.models import ItemIdentityCode
from unfold.admin import TabularInline, StackedInline
from import_export.admin import ImportExportModelAdmin
from core.admin.xlsx_export import FormattedXlsxExportMixin
from .forms import (
    ClientImportForm, ClientExportForm,
     ClientConfirmImportForm,
    QuoteItemImportForm, QuoteItemConfirmImportForm,
)
from invoices.models import AppliedTax
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter
)
from unfold.contrib.import_export.forms import  ImportForm, SelectableFieldsExportForm
from configurator.models import Configuration
from configurator.admin.configuration import AppliedTaxInline
from django.shortcuts import redirect
from django.urls import reverse, path
from unfold.decorators import action
from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.company_access import get_user_companies
from .resources import ClientResource, QuoteItemImportResource, QuoteExportResource
from .views_analysis import ConversionAnalysisView
from management.views import QuoteAcceptanceWizardView
from .views.quote_export_view import QuoteExportView
from .views.quote_dashboard_view import QuoteDashboardView
from core.tasks import send_quote_email_task
from sales.views.quote_send_email_view import QuoteSendEmailView
from company.models import EmailLog
from unfold.admin import GenericTabularInline as _GenericTabularInline
from configurator.commission.admin import QuoteCommissionInline
from configurator.commission.services import CommissionService

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


@admin.register(CommissionistAgent)
class CommissionistAgentAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "tax_identifier", "commission_percentage", "is_active", "company"]
    list_filter = ["is_active", "company"]
    search_fields = ["name", "tax_identifier", "email"]
    autocomplete_fields = ["company"]
    fieldsets = (
        (_("Agent Information"), {
            "fields": (
                ("name", "company"),
                ("tax_identifier", "commission_percentage"),
                ("email", "phone"),
                "is_active",
            ),
        }),
    )


class DiscountRuleInline(StackedInline):
    model = DiscountRule
    extra = 0
    hide_ordering_field = True
    ordering_field = "order"
    fieldsets = (
        (None, {
            "fields": (
                ("name", "is_active"),
                ("percentage", "mode", "level"),
                "condition",
            )
        }),
    )


@admin.register(DiscountPolicy)
class DiscountPolicyAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "is_active", "auto_apply", "company"]
    list_filter = ["is_active", "auto_apply", "company"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]
    inlines = [DiscountRuleInline]

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
                ("address_type", "is_default"),
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
                ("role", "department"),
                ("email", "phone", "mobile_phone"),
                ("preferred_contact_method", "address"),
                "notes",
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

class ClientAppliedTaxInline(_GenericTabularInline):
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    fields = ("tax_type", "selected_percentage", "order")
    verbose_name = _("Default tax")
    verbose_name_plural = _("Default taxes")
    tab = True

@admin.register(Client)
class ClientAdmin(_CompanyImportMixin, CompanyRestrictedAdmin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [ClientResource]
    import_form_class = ClientImportForm
    export_form_class = ClientExportForm

    list_display = ["name", "commercial_name", "tax_identifier", "industry_identifier", "phone", "address_truncated", "company",  "has_signed_terms"]
    search_fields = [
        "name", "commercial_name", "tax_identifier", "industry_identifier", "email", "phone", "address",
        "contacts__name", "contacts__email", "contacts__phone", "contacts__mobile_phone", "contacts__department",
    ]
    list_filter = ["sale_zone", "is_insured", "company", "contacts__role"]


    def get_list_filter(self, request):
        return self.list_filter

    def get_confirm_form_class(self, request):
        return ClientConfirmImportForm

    @display(description=_("Address"))
    def address_truncated(self, obj):
        if not obj.address or obj.address == "-":
            return "-"
        return obj.address if len(obj.address) <= 40 else obj.address[:40] + "…"

    autocomplete_fields = ["company", "agent", "sale_zone", "our_bank_account","discount_policies"]

    inlines = [ClientAddressInline, ClientContactInline, ClientBankAccountInline, ClientAppliedTaxInline]

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
                ("name", "commercial_name"),
                ("company",),
                ("tax_identifier", "industry_identifier"),
                ("phone", "email"),
                "address"
            )
        }),
        (_("Sales & Territory"), {
            "classes": ["tab"],
            "fields": (
                ("sale_zone", "agent"),
                "discount_policies",
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
        "address",
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


class IdentityCodeDeliveryInline(TabularInline):
    model = ItemIdentityCode
    extra = 0
    verbose_name = _("Identity Code")
    verbose_name_plural = _("Identity Codes")
    fields = ("code", "item", "delivered_at")
    readonly_fields = ("code", "item", "delivered_at")
    tab=True

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class DeliveryOrderItemInline(StackedInline):
    model = DeliveryOrderItem
    extra = 0
    tab = True
    name = _("Items")
    fieldsets = (
        (None, {
            "fields": (
                ("item_number", "description", "reference"),
                ("quantity", "unit_price"),
                ("discount_percentage", "line_total"),
                ("source_object"),
            )
        }),
    )
    readonly_fields = ["item_number", "line_total","source_object"]


@admin.register(DeliveryOrder)
class DeliveryOrderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "get_status_label", "client", "date", "subtotal_amount", "get_invoiced_label","company"]
    list_filter = [("company",AutocompleteSelectMultipleFilter), ("status",MultipleChoicesDropdownFilter), ("date",RangeDateFilter)]
    search_fields = ["number", "client__name"]
    autocomplete_fields = ["company", "client", "sales_invoice", "configuration", "project_file"]
    inlines = [DeliveryOrderItemInline]
    readonly_fields = ["number", "date", "subtotal_amount"]
    list_filter_submit = True

    fieldsets = (
        (None, {
            "fields": (
                ("number", "date"),
                ("company", "client"),
                ("project_file"),
                ("status", "subtotal_amount"),
                "sales_invoice",
                "notes",
            )
        }),
    )

    actions_row = ["create_invoice_from_do_row_action"]
    actions_detail = ["create_invoice_from_do_detail_action"]

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

    @display(
        description=_("Invoiced"),
        label={
            _("Yes"): "success",
            _("No"): "warning",
        },
    )
    def get_invoiced_label(self, obj):
        return _("Yes") if obj.sales_invoice_id else _("No")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.recalculate_subtotal()

    @action(description=_("Create Invoice"), icon="receipt_long")
    def create_invoice_from_do_row_action(self, request, object_id):
        from invoices.services.delivery_order_service import DeliveryOrderService
        from django.utils import timezone
        from django.contrib import messages as django_messages

        do = DeliveryOrder.objects.select_related("client", "company", "project_file").get(pk=object_id)

        if do.sales_invoice_id:
            django_messages.warning(request, _("This delivery order is already linked to an invoice."))
            return redirect(reverse("admin:sales_deliveryorder_changelist"))

        try:
            invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
                delivery_orders=[do],
                company=do.company,
                client=do.client,
                date_issued=timezone.now().date(),
            )
            django_messages.success(
                request,
                _("Invoice created from Delivery Order %(number)s.") % {"number": do.number},
            )
            return redirect(reverse("admin:invoices_salesinvoice_change", args=[invoice.id]))
        except Exception as e:
            django_messages.error(request, str(e))
            return redirect(reverse("admin:sales_deliveryorder_changelist"))

    @action(description=_("Create Invoice"), icon="receipt_long")
    def create_invoice_from_do_detail_action(self, request, object_id):
        return self.create_invoice_from_do_row_action(request, object_id)


@admin.register(DeliveryOrderItem)
class DeliveryOrderItemAdmin(ModelAdmin):
    list_display = ["item_number", "description", "quantity", "delivery_order"]
    search_fields = ["item_number", "description", "delivery_order__number"]
    autocomplete_fields = ["delivery_order"]
    readonly_fields = ["item_number", "line_total"]
    inlines = [IdentityCodeDeliveryInline]

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
    extra = 0
    tab = True
    autocomplete_fields = ["inventory_item"]
    readonly_fields = ["unit_price", "line_total", "total_with_tax", "cost_display", "margin_display", "margin_pct_display"]
    fields = ["inventory_item", "quantity", "discount", "unit_price", "cost_display", "line_total", "total_with_tax", "margin_display", "margin_pct_display"]
    verbose_name = _("Sale item")
    verbose_name_plural = _("Sale items")

    def _get_item_cost(self, obj):
        """Last purchase price for this inventory item, fallback to cost_price."""
        from purchasing.models import PurchaseOrderItem
        if not obj.inventory_item_id:
            return Decimal("0")
        last_po = (
            PurchaseOrderItem.objects
            .filter(inventory_item=obj.inventory_item)
            .order_by("-purchase_order__date_ordered")
            .values_list("unit_price", flat=True)
            .first()
        )
        if last_po:
            return last_po
        cp = obj.inventory_item.cost_price
        if cp and cp.amount:
            return cp.amount
        return Decimal("0")

    @display(description=_("Cost"))
    def cost_display(self, obj):
        unit_cost = self._get_item_cost(obj)
        return Money(unit_cost * (obj.quantity or Decimal("0")), "EUR")

    @display(description=_("Margin (€)"))
    def margin_display(self, obj):
        revenue = obj.line_total.amount if obj.line_total else Decimal("0")
        cost = self._get_item_cost(obj) * (obj.quantity or Decimal("0"))
        return Money(revenue - cost, "EUR")

    @display(description=_("Margin (%)"))
    def margin_pct_display(self, obj):
        revenue = obj.line_total.amount if obj.line_total else Decimal("0")
        cost = self._get_item_cost(obj) * (obj.quantity or Decimal("0"))
        if revenue:
            pct = ((revenue - cost) / revenue * 100).quantize(Decimal("0.1"))
            return f"{pct}%"
        return "—"

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
    readonly_fields = [
        "config_link", "status", 
        "cost_display", "margin_display", "margin_pct_display","subtotal_price", "total_price_display",
    ]
    fields = [
        "config_link", "status", "quote_group",
        "cost_display", 
        "margin_display", "margin_pct_display",
        "subtotal_price", "total_price_display",
    ]
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

    @display(description=_("Cost"))
    def cost_display(self, obj):
        return obj.total_cost

    @display(description=_("Margin (€)"))
    def margin_display(self, obj):
        cost = obj.total_cost.amount if obj.total_cost else Decimal("0")
        revenue = obj.subtotal_price.amount if obj.subtotal_price else Decimal("0")
        return Money(revenue - cost, "EUR")

    @display(description=_("Margin (%)"))
    def margin_pct_display(self, obj):
        cost = obj.total_cost.amount if obj.total_cost else Decimal("0")
        revenue = obj.subtotal_price.amount if obj.subtotal_price else Decimal("0")
        if revenue:
            pct = ((revenue - cost) / revenue * 100).quantize(Decimal("0.1"))
            return f"{pct}%"
        return "—"


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
class QuoteAdmin(CompanyRestrictedAdmin, FormattedXlsxExportMixin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [QuoteExportResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm
    list_display = ["id", "project_name", "client", "date_sent","date_accepted", "project_file", "show_status_customized_color", "total_price_display", "company"]
    list_filter = [
        ("status",MultipleChoicesDropdownFilter), 
        ("company",AutocompleteSelectMultipleFilter), 
        ("created_at",RangeDateFilter),
        ("date_sent", RangeDateFilter),
        ("date_accepted",RangeDateFilter)]
    search_fields = ["project_name", "client__name", "id", "external_id"]
    autocomplete_fields = ["client", "agent", "project_file", "company","discount_policies"]
    readonly_fields = [
        "subtotal_price", "tax_amount", "total_price", "tax_breakdown",
        "discount_amount", "discount_breakdown", "date_sent", "date_accepted", "date_rejected",
        "total_cost", "total_commissions_display",
        "margin_before_discount_display", "margin_before_discount_pct_display",
        "margin_after_discount_display", "margin_after_discount_pct_display",
    ]
    list_select_related = ["client", "project_file__client", "company"]
    list_filter_submit = True
    change_form_outer_after_template = "sales/quote_preset_picker.html"

    inlines = [ConfigurationReadOnlyInline, 
    QuoteInventoryItemInline,
    QuoteCommissionInline,
    QuoteConfigurationGroupInline, 
    AppliedTaxInline, 
    QuoteEmailLogInline,
    QuotePhotoInline, 
    ]


    actions_list = [ "quote_dashboard_action"]
    actions_row = ["add_configuration_to_quote", "export_quote_action", "send_email_row_action"]
    actions_detail = ["send_email_detail_action"]

    fieldsets = (
        ("Quote Details", {
            "classes": ["tab"],
            "fields": (
                ("client", "agent"),
                ("status", "company"),
                ("project_name",),
                ("date", "expires_at"),
                ("subtotal_price", "discount_amount"),
                ("tax_amount", "total_price"),
                "discount_policies",
            )
        }),
        (_("Details"), {
            "classes": ["tab"],
            "fields": (
                ("project_file", "external_id"),
                "observations",
                "discount_breakdown",
                "tax_breakdown",
            )
        }),
        (_("Status Dates"), {
            "classes": ["tab"],
            "fields": (
                ("date_sent", "date_accepted", "date_rejected"),
            )
        }),
        (_("Margin"), {
            "classes": ["tab"],
            "fields": (
                ("total_cost", "total_commissions_display"),
                ("margin_before_discount_display", "margin_before_discount_pct_display"),
                ("margin_after_discount_display", "margin_after_discount_pct_display"),
            )
        }),
    )

    @display(description=_("Total price"))
    def total_price_display(self, obj):
        return obj.total_price

    # ── Margin tab display methods ────────────────────────────────────

    def _get_quote_cost(self, obj):
        """Read the stored total_cost from the quote."""
        if obj.total_cost and hasattr(obj.total_cost, 'amount'):
            return obj.total_cost.amount
        return Decimal("0")

    def _get_total_commissions(self, obj):
        return sum(
            (c.commission_amount.amount for c in obj.commissions.all() if c.commission_amount),
            Decimal("0"),
        )

    @display(description=_("Total Commissions"))
    def total_commissions_display(self, obj):
        return Money(self._get_total_commissions(obj), "EUR")

    def _get_pre_discount_revenue(self, obj):
        """Revenue before any discounts: sum of (unit_price × qty) for all items."""
        inv_item_base = sum(
            (item.unit_price.amount * item.quantity for item in obj.inventory_items.all()),
            Decimal("0"),
        )
        quote_item_base = sum(
            (item.unit_price.amount * item.quantity for item in obj.items.all()),
            Decimal("0"),
        )
        config_subtotal = sum(
            (c.subtotal_price.amount for c in obj.configurations.all()),
            Decimal("0"),
        )
        return inv_item_base + quote_item_base + config_subtotal

    @display(description=_("Margin before discount (€)"))
    def margin_before_discount_display(self, obj):
        revenue = self._get_pre_discount_revenue(obj)
        cost = self._get_quote_cost(obj)
        commissions = self._get_total_commissions(obj)
        return Money(revenue - cost - commissions, "EUR")

    @display(description=_("Margin before discount (%)"))
    def margin_before_discount_pct_display(self, obj):
        revenue = self._get_pre_discount_revenue(obj)
        cost = self._get_quote_cost(obj)
        commissions = self._get_total_commissions(obj)
        total_cost = cost + commissions
        if total_cost:
            pct = ((revenue - total_cost) / total_cost * 100).quantize(Decimal("0.1"))
            return f"{pct}%"
        return "—"

    @display(description=_("Margin after discount (€)"))
    def margin_after_discount_display(self, obj):
        revenue = getattr(obj, 'discounted_subtotal', None)
        if revenue is None or not hasattr(revenue, 'amount'):
            revenue_amt = (obj.subtotal_price.amount if obj.subtotal_price else Decimal("0"))
        else:
            revenue_amt = revenue.amount
        cost = self._get_quote_cost(obj)
        commissions = self._get_total_commissions(obj)
        return Money(revenue_amt - cost - commissions, "EUR")

    @display(description=_("Margin after discount (%)"))
    def margin_after_discount_pct_display(self, obj):
        revenue = getattr(obj, 'discounted_subtotal', None)
        if revenue is None or not hasattr(revenue, 'amount'):
            revenue_amt = (obj.subtotal_price.amount if obj.subtotal_price else Decimal("0"))
        else:
            revenue_amt = revenue.amount
        cost = self._get_quote_cost(obj)
        commissions = self._get_total_commissions(obj)
        total_cost = cost + commissions
        if total_cost:
            pct = ((revenue_amt - total_cost) / total_cost * 100).quantize(Decimal("0.1"))
            return f"{pct}%"
        return "—"

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
        CommissionService.prepopulate_for_quote(form.instance)
        CommissionService.recalculate(form.instance)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        from sales.forms import QuotePresetPickerForm
        extra_context = extra_context or {}
        extra_context["preset_picker_form"] = QuotePresetPickerForm()
        return super().change_view(request, object_id, form_url, extra_context)

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
            path(
                "<int:quote_id>/send-email/",
                self.admin_site.admin_view(QuoteSendEmailView.as_view(model_admin=self)),
                name="sales_quote_send_email",
            ),
        ]
        return custom + urls


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
        url = reverse("admin:sales_quote_send_email", args=[object_id])
        return redirect(f"{url}?redirect_to=changelist")

    @action(description=_("Send Email"), icon="mail")
    def send_email_detail_action(self, request, object_id):
        return redirect(reverse("admin:sales_quote_send_email", args=[object_id]))


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
