from unfold.admin import GenericStackedInline
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.contrib.filters.admin.dropdown_filters import MultipleChoicesDropdownFilter
from unfold.contrib.inlines.admin import NonrelatedTabularInline

from decimal import Decimal
from djmoney.money import Money
from management.models import ProjectFile, ProjectFileStatus, ResourcePlanning, ResourcePlanningBacklogItem
from management.forms import ProjectFileAdminForm
from management.resources import ProjectFileResource
from django.contrib import admin, messages
from django.db.models import Q, Sum
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from unfold.admin import ModelAdmin, StackedInline, TabularInline
from unfold.decorators import action, display
from import_export.admin import ExportActionModelAdmin
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from sales.models import Quote, DeliveryOrder
from production.models import ProductionTicket
from invoices.models import SalesInvoice, Invoice, AppliedTax
from purchasing.models import PurchaseOrder
from invoices.views.invoice.project_file_wizard_views import ProjectFileInvoiceWizardView
from invoices.views.invoice.delivery_order_wizard_views import DeliveryOrderWizardView
from invoices.views.invoice.bulk_invoice_views import BulkInvoiceConfirmView
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.views.project_file_export import ProjectFileRepairOrderExportView
from core.admin.company_admin import CompanyRestrictedAdmin
from configurator.models import Configuration
from company.models import WorkingZone
from management.models import WorkEntry
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter
)


class QuoteInline(TabularInline):
    model = Quote
    extra = 0
    tab = True
    tab_title = _("Quotes")
    fields = ["id", "project_name", "status", "discounted_subtotal", "total_price"]
    readonly_fields = ["id", "project_name", "status", "discounted_subtotal", "total_price"]
    show_change_link = True

    def save_new_instance(self, parent, instance):
        pass

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

class SalesInvoicesInline(NonrelatedTabularInline):
    """
    Read-only, navigation-only inline listing every sales invoice touching
    this project file — both directly-linked (single-PF) and bulk
    (multi-PF) invoices — consolidated into one list instead of two
    separate inline sections.
    """
    model = SalesInvoice
    extra = 0
    max_num = 0
    can_delete = False
    tab = True
    tab_title = _("Sales Invoices")
    fields = ["invoice_link", "date_issued", "pf_amount", "total_amount", "status_display"]
    readonly_fields = ["invoice_link", "date_issued", "pf_amount", "total_amount", "status_display"]

    def get_form_queryset(self, obj):
        self._project_file = obj
        return SalesInvoice.objects.filter(
            Q(project_file=obj) | Q(project_files=obj)
        ).distinct().order_by("-date_issued")

    def save_new_instance(self, parent, instance):
        pass

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @display(description=_("Invoice Number"))
    def invoice_link(self, obj):
        label = obj.invoice_number or f"Pro Forma #{obj.id}"
        url = reverse("admin:invoices_salesinvoice_change", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, label)

    @display(description=_("This PF"))
    def pf_amount(self, obj):
        project_file = getattr(self, "_project_file", None)
        total = None
        if project_file is not None:
            total = obj.items.filter(
                project_file_id=project_file.pk,
            ).aggregate(s=Sum("line_total"))["s"]
            if total and hasattr(total, "amount"):
                total = total.amount
        if not total:
            # Not item-tagged for this PF (direct single-PF invoice) — the
            # whole invoice belongs to this project file.
            return obj.total_amount
        return Money(total, obj.total_amount_currency)

    @display(description=_("Status"))
    def status_display(self, obj):
        return obj.get_status_display()

class ProductionTicketInline(TabularInline):
    model = ProductionTicket
    extra = 0
    tab = True
    tab_title = _("Production")
    fields = ["pk", "status", "created_at"]
    readonly_fields = ["pk", "status", "created_at"]
    show_change_link = True

    def save_new_instance(self, parent, instance):
        pass

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

class DeliveryOrderInline(TabularInline):
    model = DeliveryOrder
    extra = 0
    tab = True
    tab_title = _("Logistics")
    fields = ["number", "date", "status"]
    readonly_fields = ["number", "date", "status"]
    show_change_link = True

    def save_new_instance(self, parent, instance):
        pass

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

class PurchaseInvoiceInline(TabularInline):
    model = Invoice
    extra = 0
    tab = True
    tab_title = _("Expenses")
    fields = ["invoice_number", "provider", "total_amount", "date_issued"]
    readonly_fields = ["invoice_number", "provider", "total_amount", "date_issued"]
    show_change_link = True


    def save_new_instance(self, parent, instance):
        pass

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

class PurchaseOrderInline(TabularInline):
    model = PurchaseOrder
    extra = 0
    tab = True
    tab_title = _("Purchase Orders")
    fields = ["number", "provider", "get_status_label", "total_amount", "date_ordered"]
    readonly_fields = ["number", "provider", "get_status_label", "total_amount", "date_ordered"]
    show_change_link = True

    @display(description=_("Status"))
    def get_status_label(self, obj):
        return obj.get_status_display()

    def save_new_instance(self, parent, instance):
        pass

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

class DirectConfigurationInline(TabularInline):
    model = Configuration
    fk_name = "project_file"
    extra = 0
    tab = True
    tab_title = _("Direct Work")
    fields = ["name", "product", "status", "subtotal_price", "total_price"]
    readonly_fields = ["name", "product", "status", "subtotal_price", "total_price"]
    show_change_link = True

    def get_queryset(self, request):
        return super().get_queryset(request).filter(quote__isnull=True)

class ProjectFileWorkingZoneInline(StackedInline):
    model = WorkingZone
    fk_name = "project_file"
    extra = 1
    tab = True
    tab_title = _("Working Zones")
    fields = ["name", "zone_type", "street_address", "city", "postal_code", "province", "country", "location", "is_active"]
    readonly_fields = ["location_confirmed_at"]
    validate_min = True

    def get_min_num(self, request, obj=None, **kwargs):
        # Only require a zone when creating a brand-new ProjectFile — existing
        # records (or ones created before this flag existed) must stay
        # editable even with zero zones, matching require_asset_on_project_file.
        if obj and obj.pk:
            return 0
        company = self._company_from_request(request)
        if company and company.require_working_zone_on_project_file:
            return 1
        return 0

    def _company_from_request(self, request):
        company_id = request.POST.get("company") or request.GET.get("company")
        if not company_id:
            return None
        from company.models import Company
        return Company.objects.filter(pk=company_id).first()


class WorkEntryAppliedTaxInline(GenericStackedInline):
    model = AppliedTax
    extra = 0
    collapsible = True
    autocomplete_fields = ("tax_type",)
    fieldsets = (
        (_("Tax"), {
            "fields": ("tax_type", "selected_percentage"),
        }),
        (_("Computed"), {
            "fields": ("base_amount", "calculated_amount"),
        }),
    )
    readonly_fields = ("tax_type", "selected_percentage", "base_amount", "calculated_amount")
    verbose_name = _("Applied Tax")
    verbose_name_plural = _("Applied Taxes")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class WorkEntryInline(TabularInline):
    model = WorkEntry
    extra = 0
    tab = True
    tab_title = _("Work Entries")
    fields = ["entry_type", "description", "quantity", "unit_price", "total_price", "total_with_tax", "date", "source_link", "movement_link"]
    readonly_fields = ["total_price", "total_with_tax", "source_link", "movement_link"]
    autocomplete_fields = ["entry_type"]
    inlines = [WorkEntryAppliedTaxInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("entry_type", "source_content_type")

    @admin.display(description=_("Source Transaction"))
    def source_link(self, obj):
        from assets.models import AssetTransaction

        source = obj.source
        if not isinstance(source, AssetTransaction):
            return "—"
        url = reverse("admin:assets_assettransaction_change", args=[source.pk])
        return format_html('<a href="{}">{}</a>', url, source)

    @admin.display(description=_("Related Movement"))
    def movement_link(self, obj):
        from assets.models import AssetTransaction
        from assets.services.movement_context import get_related_movement

        source = obj.source
        if not isinstance(source, AssetTransaction):
            return "—"
        movement = get_related_movement(source)
        if not movement:
            return "—"
        url = reverse("admin:assets_assettransaction_change", args=[movement.pk])
        return format_html('<a href="{}">{}</a>', url, movement)


@admin.register(ProjectFile)
class ProjectFileAdmin(CompanyRestrictedAdmin, ModelAdmin, ExportActionModelAdmin):
    resource_classes = [ProjectFileResource]
    export_form_class = SelectableFieldsExportForm
    list_display = ["reference", "name", "client", "show_status_customized_color", "total_amount", "total_invoiced", "total_purchased", "start_date", "invoicing_status"]
    list_filter = [("client", AutocompleteSelectMultipleFilter), ("status", MultipleChoicesDropdownFilter), ("company", AutocompleteSelectMultipleFilter), ("created_at", RangeDateFilter)]
    search_fields = ["reference", "name", "client__name"]
    autocomplete_fields = ["client", "company", "asset"]
    form = ProjectFileAdminForm
    list_select_related = ["client"]
    list_filter_submit = True
    readonly_fields = ["reference", "incremental_id", "invoicing_summary_display"]

    inlines = [
        QuoteInline,
        ProjectFileWorkingZoneInline,
        DirectConfigurationInline,
        WorkEntryInline,
        SalesInvoicesInline,
        ProductionTicketInline,
        DeliveryOrderInline,
        PurchaseOrderInline,
        PurchaseInvoiceInline,
    ]

    actions_row = ["create_invoice_row_action", "create_delivery_order_row_action", "repair_order_row_action"]
    actions_detail = ["create_invoice_detail_action", "create_delivery_order_detail_action", "asset_reception_detail_action", "repair_order_detail_action"]
    actions = ["create_bulk_invoice"]

    fieldsets = (
        (_("Basic Information"), {
            "fields": (
                ("reference", "company"),
                ("name", "client"),
                ("asset", "status"),
                "description",
                "notification_email",
            )
        }),
        (_("Invoicing"), {
            "fields": (
                "invoicing_summary_display",
            )
        }),
        (_("Timeline"), {
            "fields": (
                ("start_date", "end_date"),
            )
        }),
        (_("Internal Data"), {
            "classes": ("collapse",),
            "fields": (
                "incremental_id",
            )
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Adopt orphan assets: linking one to an expediente ties it to the client.
        asset = obj.asset
        if asset and not asset.client_id and obj.client_id:
            asset.client_id = obj.client_id
            asset.save(update_fields=["client"])

    def save_formset(self, request, form, formset, change):
        if formset.model is WorkingZone:
            instances = formset.save(commit=False)
            for obj in formset.deleted_objects:
                obj.delete()
            for instance in instances:
                instance.client = form.instance.client
                instance.company = form.instance.company
                instance.save()
            formset.save_m2m()
        else:
            formset.save()

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:project_file_id>/create-invoice/",
                self.admin_site.admin_view(
                    ProjectFileInvoiceWizardView.as_view(model_admin=self)
                ),
                name="management_projectfile_create_invoice",
            ),
            path(
                "<int:project_file_id>/create-delivery-order/",
                self.admin_site.admin_view(
                    DeliveryOrderWizardView.as_view(model_admin=self)
                ),
                name="management_projectfile_create_delivery_order",
            ),
            path(
                "bulk-invoice/",
                self.admin_site.admin_view(
                    BulkInvoiceConfirmView.as_view(model_admin=self)
                ),
                name="management_projectfile_bulk_invoice",
            ),
            path(
                "<int:project_file_id>/repair-order/",
                self.admin_site.admin_view(
                    ProjectFileRepairOrderExportView.as_view(model_admin=self)
                ),
                name="management_projectfile_repair_order",
            ),
        ]
        return custom + urls

    @action(description=_("Create Invoice"), icon="receipt_long")
    def create_invoice_row_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_create_invoice", args=[object_id])
        )

    @action(description=_("Create Invoice"), icon="receipt_long")
    def create_invoice_detail_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_create_invoice", args=[object_id])
        )

    @action(description=_("Create Delivery Order"), icon="local_shipping")
    def create_delivery_order_row_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_create_delivery_order", args=[object_id])
        )

    @action(description=_("Create Delivery Order"), icon="local_shipping")
    def create_delivery_order_detail_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_create_delivery_order", args=[object_id])
        )

    @action(description=_("Repair Order"), icon="build", attrs={"target": "_blank"})
    def repair_order_row_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_repair_order", args=[object_id])
        )

    @action(description=_("Repair Order"), icon="build", attrs={"target": "_blank"})
    def repair_order_detail_action(self, request, object_id):
        return redirect(
            reverse("admin:management_projectfile_repair_order", args=[object_id])
        )

    @action(description=_("Asset Reception"), icon="garage")
    def asset_reception_detail_action(self, request, object_id):
        pf = ProjectFile.objects.select_related("company").get(pk=object_id)
        product = pf.company.asset_reception_product if pf.company else None
        if not product:
            messages.warning(
                request,
                _("This company has no asset reception product configured. "
                  "Set it on the company settings first."),
            )
            return redirect(
                reverse("admin:management_projectfile_change", args=[object_id])
            )
        url = reverse(
            "admin:configurator_configuration_wizard_form", args=[product.pk]
        )
        return redirect(f"{url}?project_file_id={object_id}")

    @action(description=_("Create Bulk Invoice"), icon="receipt_long")
    def create_bulk_invoice(self, request, queryset):
        pf_list = list(queryset)
        if len(pf_list) < 2:
            messages.warning(request, _("Select at least 2 project files for bulk invoicing."))
            return None

        client_ids = {pf.client_id for pf in pf_list}
        if len(client_ids) > 1:
            messages.error(request, _("All selected project files must belong to the same client."))
            return None

        request.session["bulk_invoice_pf_ids"] = [pf.pk for pf in pf_list]
        return redirect(reverse("admin:management_projectfile_bulk_invoice"))

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(ProjectFileStatus.NEW.label): "info",  # blue
            _(ProjectFileStatus.ACTIVE.label): "success",  # green
            _(ProjectFileStatus.ON_HOLD.label): "warning",  # orange
            _(ProjectFileStatus.COMPLETED.label): "primary",  # purple
            _(ProjectFileStatus.CANCELLED.label): "danger",  # red
        },
    )
    def show_status_customized_color(self, obj):
        return _(obj.get_status_display())

    @display(description=_("Invoicing"))
    def invoicing_status(self, obj):
        """List display column — reads from cached model fields."""
        pct = obj.percentage_invoiced or Decimal("0")
        if obj.total_amount.amount <= 0:
            return "—"

        if pct >= 100:
            return format_html(
                '<span style="color: #16a34a; font-weight: 600;">✓ {}%</span>',
                pct
            )
        elif pct > 0:
            return format_html(
                '<span style="color: #2563eb; font-weight: 600;">{}%</span>',
                pct
            )
        return format_html(
            '<span style="color: #9ca3af;">0%</span>'
        )


    @admin.display(description=_("Invoicing Summary"))
    def invoicing_summary_display(self, obj):
        """Detail page read-only field showing full invoicing breakdown (subtotal-based)."""
        if not obj.pk:
            return "—"

        summary = ProjectFileInvoiceService.get_invoiceable_summary(obj)
        if summary['total_invoiceable_subtotal_net'] <= 0:
            return format_html(
                '<span style="color: #6b7280;">{}</span>',
                _("No accepted quotes or direct configurations yet.")
            )

        # Net-of-discount figures: comparable to invoice subtotals regardless
        # of whether the discount was applied per-line or invoice-level.
        invoiceable = summary['total_invoiceable_subtotal_net']
        invoiced = summary['total_invoiced_subtotal_net']
        remaining = summary['remaining_subtotal_net']
        pct = (
            (invoiced / invoiceable * Decimal("100")).quantize(Decimal("0.01"))
            if invoiceable > 0 else Decimal("0.00")
        )

        html = f"""
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; max-width: 600px;">
            <div style="background: #f9fafb; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #6b7280; margin-bottom: 4px;">{_("Total Invoiceable (subtotal)")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #111827;">{invoiceable:.2f} €</div>
            </div>
            <div style="background: #eff6ff; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #2563eb; margin-bottom: 4px;">{_("Already Invoiced (subtotal)")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #1d4ed8;">{invoiced:.2f} €</div>
            </div>
            <div style="background: #f0fdf4; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #16a34a; margin-bottom: 4px;">{_("Remaining (subtotal)")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #15803d;">{remaining:.2f} €</div>
            </div>
        </div>
        <div style="margin-top: 8px;">
            <div style="background: #e5e7eb; border-radius: 999px; height: 8px; overflow: hidden;">
                <div style="background: #2563eb; height: 100%; width: {min(pct, 100)}%; border-radius: 999px;"></div>
            </div>
            <div style="font-size: 11px; color: #6b7280; margin-top: 4px;">{pct}% {_("invoiced")}</div>
        </div>
        """
        return format_html(html)
