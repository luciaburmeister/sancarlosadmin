from unfold.admin import GenericStackedInline
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.contrib.filters.admin.dropdown_filters import MultipleChoicesDropdownFilter
from unfold.contrib.inlines.admin import NonrelatedStackedInline, NonrelatedTabularInline

from decimal import Decimal
from djmoney.money import Money
from configurator.templatetags.formatting import format_hours_minutes
from management.models import (
    ProjectFile,
    ProjectFileDeliveryStatus,
    ProjectFileProductionStatus,
    ProjectFileStatus,
    ResourcePlanning,
    ResourcePlanningBacklogItem,
)
from management.admin.filters import ProjectFileCreatedByFilter, ProjectFileInvoicingFilter
from management.forms import ProjectFileAdminForm
from management.resources import ProjectFileResource
from django.contrib import admin, messages
from django.db.models import Case, IntegerField, Q, Sum, Value, When
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html, format_html_join
from django.utils.text import capfirst
from django.utils.timezone import localtime
from unfold.admin import ModelAdmin, StackedInline, TabularInline
from unfold.decorators import action, display
from import_export.admin import ExportActionModelAdmin
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from sales.models import Quote, DeliveryOrder
from production.models import ProductionTicket
from invoices.models import (
    AppliedTax,
    Invoice,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceStatus,
)
from purchasing.models import PurchaseOrder
from invoices.views.invoice.project_file_wizard_views import (
    ProjectFileAdvancePreviewView,
    ProjectFileInvoiceWizardView,
)
from invoices.views.invoice.delivery_order_wizard_views import DeliveryOrderWizardView
from invoices.views.invoice.bulk_invoice_views import BulkInvoiceConfirmView
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.services.project_file_status import get_pending_delivery_lines
from management.views.project_file_export import ProjectFileRepairOrderExportView
from core.admin.company_admin import CompanyRestrictedAdmin
from configurator.models import Configuration
from company.models import WorkingZone
from management.models import WorkEntry, CostEntry
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter
)


def _pill(text, color):
    return format_html(
        '<span style="display:inline-block;padding:1px 8px;border-radius:999px;'
        'background:{};color:white;font-size:11px;font-weight:600;">{}</span>',
        color, text,
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
    fields = ["invoice_link", "type_display", "date_issued", "pf_amount", "total_amount", "status_display"]
    readonly_fields = ["invoice_link", "type_display", "date_issued", "pf_amount", "total_amount", "status_display"]

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
        label = obj.display_number
        url = reverse("admin:invoices_salesinvoice_change", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, label)

    @display(description=_("Type"))
    def type_display(self, obj):
        return _pill(
            obj.get_document_type_display(),
            "#2563eb" if obj.document_type == SalesInvoiceDocumentType.ADVANCE else "#6b7280",
        )

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
    fields = [
        "name", "zone_type", "cnae", "nima",
        "street_address", "city", "postal_code", "province", "country",
        "location", "is_active",
    ]
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


class ProjectFileEntryInlineMixin:
    """Shared bits of the Work Entry / Cost Entry inlines: source links and
    an entry_type autocomplete restricted to the inline model's direction."""

    extra = 0
    tab = True
    autocomplete_fields = ["entry_type"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("entry_type", "source_content_type")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "entry_type":
            kwargs["queryset"] = db_field.remote_field.model.objects.filter(
                direction=self.model.DIRECTION,
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

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


class WorkEntryInline(ProjectFileEntryInlineMixin, TabularInline):
    model = WorkEntry
    tab_title = _("Work Entries")
    fields = ["entry_type", "description", "quantity", "unit_price", "total_price", "total_with_tax", "date", "is_locked", "source_link", "movement_link"]
    readonly_fields = ["total_price", "total_with_tax", "is_locked", "source_link", "movement_link"]
    inlines = [WorkEntryAppliedTaxInline]


class CostEntryInline(ProjectFileEntryInlineMixin, TabularInline):
    model = CostEntry
    tab_title = _("Cost Entries")
    fields = ["entry_type", "description", "quantity", "unit_price", "total_price", "date", "source_link", "movement_link"]
    readonly_fields = ["total_price", "source_link", "movement_link"]


class LaborHoursMarginInline(NonrelatedStackedInline):
    """Read-only top-level tab: billed-vs-worked labor margin summary plus the
    expediente's production tickets. Backed by the ProjectFile itself as its
    own single "row" -- there's no separate related model for this content,
    and NonrelatedStackedInline doesn't require a real FK to the parent, so
    this is the only way to get a genuine top-level tab (alongside Quotes,
    Production, etc.) for computed/derived content instead of nesting it
    inside the General tab's fieldset area.
    """
    model = ProjectFile
    tab = True
    # This Unfold version doesn't read tab_title -- the tab label comes from
    # the inline's own verbose_name(_plural), which would otherwise be
    # "Project File(s)" since this inline is (necessarily) self-referencing.
    verbose_name = _("Labor Hours Margin")
    verbose_name_plural = _("Labor Hours Margin")
    extra = 0
    max_num = 1
    can_delete = False
    fields = ["labor_hours_margin_summary_display", "production_tickets_summary_display"]
    readonly_fields = ["labor_hours_margin_summary_display", "production_tickets_summary_display"]

    def get_form_queryset(self, obj):
        return ProjectFile.objects.filter(pk=obj.pk)

    def save_new_instance(self, parent, instance):
        pass

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Labor Hours Margin"))
    def labor_hours_margin_summary_display(self, obj):
        """Billed labor hours vs. real hours worked, and the gap."""
        if not obj.pk:
            return "—"

        summary = obj.labor_hours_margin_summary()
        if not summary.has_labor_configs:
            return format_html(
                '<span style="color: #6b7280;">{}</span>',
                _("No labor-hours configurations on this project file."),
            )

        billed_hours = summary.billed_hours
        real_hours = summary.real_hours
        gap_hours = summary.gap_hours
        extra_margin = summary.extra_margin
        deviation_pct = summary.deviation_pct

        # Real Hours, Margin, and Deviation share one neutral color -- kept
        # uniform on purpose, not colored by sign (green/red), so the row
        # reads as one consistent system rather than three different signals.
        uniform_bg, uniform_label_color, uniform_value_color = "#eff6ff", "#2563eb", "#1d4ed8"

        html = f"""
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; max-width: 760px;">
            <div style="background: #f9fafb; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: #6b7280; margin-bottom: 4px;">{_("Billed Hours")}</div>
                <div style="font-size: 18px; font-weight: 700; color: #111827;">{format_hours_minutes(billed_hours)}</div>
            </div>
            <div style="background: {uniform_bg}; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: {uniform_label_color}; margin-bottom: 4px;">{_("Real Hours Worked")}</div>
                <div style="font-size: 18px; font-weight: 700; color: {uniform_value_color};">{format_hours_minutes(real_hours)}</div>
            </div>
            <div style="background: {uniform_bg}; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: {uniform_label_color}; margin-bottom: 4px;">{_("Extra Margin")}</div>
                <div style="font-size: 18px; font-weight: 700; color: {uniform_value_color};">{format_hours_minutes(gap_hours)} · {extra_margin:.2f} €</div>
            </div>
            <div style="background: {uniform_bg}; border-radius: 8px; padding: 12px; text-align: center;">
                <div style="font-size: 11px; color: {uniform_label_color}; margin-bottom: 4px;">{_("% Deviation")}</div>
                <div style="font-size: 18px; font-weight: 700; color: {uniform_value_color};">{deviation_pct:.1f}%</div>
            </div>
        </div>
        """
        return format_html(html)

    @admin.display(description=_("Production Hours"))
    def production_tickets_summary_display(self, obj):
        """Every production ticket on this expediente, with hours worked,
        linking to each ticket's own page."""
        if not obj.pk:
            return "—"

        tickets = obj.production_tickets_summary()
        if not tickets:
            return format_html(
                '<span style="color: #6b7280;">{}</span>',
                _("No production tickets on this project file."),
            )

        rows = format_html_join(
            "",
            '<tr>'
            '<td style="padding: 6px 12px 6px 0;"><a href="{}">{}</a></td>'
            '<td style="padding: 6px 12px;">{}</td>'
            '<td style="padding: 6px 12px;">{}</td>'
            '<td style="padding: 6px 0 6px 12px; text-align: right;">{}</td>'
            '</tr>',
            (
                (
                    reverse("admin:production_productionticket_change", args=[entry["ticket"].pk]),
                    f"Ticket #{entry['ticket'].pk}",
                    entry["ticket"].last_stage_label or "—",
                    entry["ticket"].get_status_display(),
                    format_hours_minutes(entry["worked_hours"]),
                )
                for entry in tickets
            ),
        )

        return format_html(
            '<table style="width: 100%; max-width: 760px; font-size: 13px; color: #374151; border-collapse: collapse;">'
            '<thead><tr style="text-align: left; color: #6b7280; font-size: 11px;">'
            '<th style="padding: 0 12px 6px 0;">{}</th><th style="padding: 0 12px 6px;">{}</th>'
            '<th style="padding: 0 12px 6px;">{}</th><th style="padding: 0 0 6px 12px; text-align: right;">{}</th>'
            '</tr></thead>'
            '<tbody>{}</tbody>'
            '</table>',
            _("Ticket"), _("Last Stage"), _("Status"), _("Hours"),
            rows,
        )


@admin.register(ProjectFile)
class ProjectFileAdmin(CompanyRestrictedAdmin, ModelAdmin, ExportActionModelAdmin):
    resource_classes = [ProjectFileResource]
    export_form_class = SelectableFieldsExportForm
    list_display = ["reference", "name", "client", "show_status_customized_color", "show_operational_status", "total_amount", "total_invoiced", "total_purchased", "total_costs", "start_date", "invoicing_status"]
    list_filter = [
        ("client", AutocompleteSelectMultipleFilter),
        ("status", MultipleChoicesDropdownFilter),
        ("delivery_status", MultipleChoicesDropdownFilter),
        ("production_status", MultipleChoicesDropdownFilter),
        ProjectFileInvoicingFilter,
        ProjectFileCreatedByFilter,
        ("company", AutocompleteSelectMultipleFilter),
        ("created_at", RangeDateFilter),
    ]
    search_fields = ["reference", "name", "client__name","client__commercial_name"]
    autocomplete_fields = ["client", "company", "asset"]
    form = ProjectFileAdminForm
    list_select_related = ["client"]
    list_filter_submit = True
    readonly_fields = ["reference", "incremental_id", "show_delivery_status", "show_production_status", "pending_delivery_display", "invoicing_summary_display", "production_hours_summary_display"]

    inlines = [
        QuoteInline,
        ProjectFileWorkingZoneInline,
        DirectConfigurationInline,
        WorkEntryInline,
        CostEntryInline,
        SalesInvoicesInline,
        ProductionTicketInline,
        LaborHoursMarginInline,
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
                ("show_production_status", "show_delivery_status"),
                "description",
                "notification_email",
            )
        }),
        (_("Pending Delivery"), {
            "fields": (
                "pending_delivery_display",
            )
        }),
        (_("Invoicing"), {
            "fields": (
                "invoicing_summary_display",
            )
        }),
        (_("Production Hours"), {
            "fields": (
                "production_hours_summary_display",
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
        # Mirrors ProjectFile.operational_status precedence so the Progress
        # column can be sorted: delivery wins as soon as anything has shipped.
        return super().get_queryset(request).annotate(
            _operational_rank=Case(
                When(delivery_status=ProjectFileDeliveryStatus.DELIVERED, then=Value(4)),
                When(delivery_status=ProjectFileDeliveryStatus.PARTIALLY_DELIVERED, then=Value(3)),
                When(
                    production_status__in=[
                        ProjectFileProductionStatus.PENDING,
                        ProjectFileProductionStatus.IN_PROGRESS,
                    ],
                    then=Value(1),
                ),
                When(delivery_status=ProjectFileDeliveryStatus.NOT_DELIVERED, then=Value(2)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )

    def has_delete_permission(self, request, obj=None):
        # An advance invoice always keeps its expediente
        if obj is not None and obj.has_advance_invoices():
            return False
        return super().has_delete_permission(request, obj)

    def get_deleted_objects(self, objs, request):
        # Their advance invoices protect them: the delete confirmation lists
        # them and deletes nothing (bulk "Delete selected" included).
        deleted, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        advances = SalesInvoice.objects.filter(
            project_file__in=[obj.pk for obj in objs],
            document_type=SalesInvoiceDocumentType.ADVANCE,
        ).order_by("pk")
        protected = list(protected) + [
            f"{capfirst(SalesInvoice._meta.verbose_name)}: {advance}" for advance in advances
        ]
        return deleted, model_count, perms_needed, protected

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
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
                "<int:project_file_id>/create-invoice/advance-preview/",
                self.admin_site.admin_view(
                    ProjectFileAdvancePreviewView.as_view(model_admin=self)
                ),
                name="management_projectfile_advance_preview",
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
            ProjectFileStatus.NEW: "info",  # blue
            ProjectFileStatus.ACTIVE: "success",  # green
            ProjectFileStatus.ON_HOLD: "warning",  # orange
            ProjectFileStatus.COMPLETED: "primary",  # purple
            ProjectFileStatus.CANCELLED: "danger",  # red
        },
    )
    def show_status_customized_color(self, obj):
        return obj.status, obj.get_status_display()

    @display(
        description=_("Progress"),
        ordering="_operational_rank",
        label={
            "in_production": "warning",
            "ready_to_deliver": "info",
            "partially_delivered": "info",
            "delivered": "success",
            "nothing_pending": "",
        },
    )
    def show_operational_status(self, obj):
        """Combined production + delivery view, derived on the model."""
        return obj.operational_status

    @display(
        description=_("Delivery"),
        ordering="delivery_status",
        label={
            ProjectFileDeliveryStatus.NOT_APPLICABLE: "",
            ProjectFileDeliveryStatus.NOT_DELIVERED: "warning",
            ProjectFileDeliveryStatus.PARTIALLY_DELIVERED: "info",
            ProjectFileDeliveryStatus.DELIVERED: "success",
        },
    )
    def show_delivery_status(self, obj):
        return obj.delivery_status, obj.get_delivery_status_display()

    @display(
        description=_("Production"),
        ordering="production_status",
        label={
            ProjectFileProductionStatus.NO_PRODUCTION: "",
            ProjectFileProductionStatus.PENDING: "warning",
            ProjectFileProductionStatus.IN_PROGRESS: "info",
            ProjectFileProductionStatus.DONE: "success",
        },
    )
    def show_production_status(self, obj):
        return obj.production_status, obj.get_production_status_display()

    @display(description=_("Invoicing"))
    def invoicing_status(self, obj):
        """List display column — reads from cached model fields."""
        pct = obj.percentage_invoiced or Decimal("0")
        if obj.total_amount.amount <= 0:
            return "—"

        # The invoiced % counts advances not deducted yet: show them apart
        advance_pending = obj.advance_pending_amount.amount
        if advance_pending > Decimal("0.01"):
            advance_pct = (
                advance_pending / obj.total_amount.amount * Decimal("100")
            ).quantize(Decimal("0.01"))
            goods_pct = max(pct - advance_pct, Decimal("0.00"))
            return format_html(
                '<span style="color: {}; font-weight: 600;">{}%</span> '
                '<span style="color: #d97706; font-weight: 600;">{}</span>',
                "#2563eb" if goods_pct > 0 else "#9ca3af",
                goods_pct,
                _("+%(percentage)s%% advance") % {"percentage": advance_pct},
            )

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


    @admin.display(description=_("Items not yet delivered"))
    def pending_delivery_display(self, obj):
        """Detail page read-only field: deliverable lines still owed to the
        client. Only sent/delivered orders count as shipped (same rule as the
        derived delivery status); draft quantities are shown apart. Inventory
        lines show the item's stock on hand and incoming purchase quantity;
        the pending figure turns red when those two cannot cover it."""
        if not obj.pk:
            return "—"

        lines = get_pending_delivery_lines(obj)
        if not lines:
            return format_html(
                '<span style="color: #6b7280;">{}</span>',
                _("No accepted quotes or direct configurations yet."),
            )

        pending = [line for line in lines if line["pending"] > 0]
        if not pending:
            return format_html(
                '<span style="color: #16a34a; font-weight: 600;">✓ {}</span>',
                _("Everything has been delivered."),
            )

        def qty(value):
            return f"{value.normalize():f}"

        header = format_html(
            '<tr style="font-size: 11px; color: #6b7280; text-align: left;">'
            '<th style="padding: 4px 6px 4px 0; font-weight: 500;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '<th style="padding: 4px 0 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '</tr>',
            _("Reference"), _("Item"), _("Ordered"), _("Shipped"), _("In draft"),
            _("Stock"), _("Incoming"), _("Pending"),
        )

        def pending_color(line):
            """Red when stock plus what is on the way cannot cover the line."""
            if line["stock"] is None:
                return "#b45309"
            if line["stock"] + line["incoming"] < line["pending"]:
                return "#dc2626"
            return "#b45309"
        rows = format_html_join(
            "",
            '<tr style="border-top: 1px solid #e5e7eb;">'
            '<td style="padding: 4px 6px 4px 0; color: #6b7280;">{}</td>'
            '<td style="padding: 4px 6px;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right; color: #d97706;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right; color: #2563eb;">{}</td>'
            '<td style="padding: 4px 0 4px 6px; text-align: right; font-weight: 600; color: {};">{}</td>'
            '</tr>',
            (
                (
                    line["reference"] or "—",
                    line["description"],
                    qty(line["ordered"]),
                    qty(line["shipped"]),
                    qty(line["in_draft"]) if line["in_draft"] > 0 else "—",
                    qty(line["stock"]) if line["stock"] is not None else "—",
                    qty(line["incoming"]) if line["incoming"] else "—",
                    pending_color(line),
                    qty(line["pending"]),
                )
                for line in pending
            ),
        )
        delivered_count = len(lines) - len(pending)
        footer = format_html(
            '<div style="font-size: 11px; color: #6b7280; margin-top: 6px;">{} {}</div>',
            delivered_count,
            _("line(s) fully delivered (not listed)"),
        ) if delivered_count else ""

        return format_html(
            '<div style="border: 1px solid #fde68a; background: #fffbeb; border-radius: 8px; padding: 10px 12px; max-width: 960px;">'
            '<div style="font-size: 13px; font-weight: 600; color: #92400e; margin-bottom: 6px;">{} {}</div>'
            '<table style="width: 100%; font-size: 12px; color: #374151;">{}{}</table>'
            '{}'
            '</div>',
            len(pending),
            _("line(s) pending delivery"),
            header,
            rows,
            footer,
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
        if not summary['advances']:
            return format_html(html)
        return format_html("{}{}", format_html(html), self._advances_summary_html(summary))

    def _advances_summary_html(self, summary):
        """The expediente's advance invoices (cancelled ones left out): what
        they billed, what later invoices deducted of it and what is left to
        deduct, then one row per advance."""
        advances = summary['advances']
        advanced = sum((a['base'] for a in advances), Decimal("0.00"))
        deducted = sum((a['deducted'] for a in advances), Decimal("0.00"))
        pending = summary['advances_open_net']

        tiles = format_html_join(
            "",
            '<div style="background: {}; border-radius: 8px; padding: 12px; text-align: center;">'
            '<div style="font-size: 11px; color: {}; margin-bottom: 4px;">{}</div>'
            '<div style="font-size: 18px; font-weight: 700; color: {};">{} €</div>'
            '</div>',
            (
                ("#f5f3ff", "#7c3aed", _("Advanced"), "#6d28d9", f"{advanced:.2f}"),
                ("#f5f3ff", "#7c3aed", _("Deducted"), "#6d28d9", f"{deducted:.2f}"),
                ("#fffbeb", "#d97706", _("Pending to deduct"), "#b45309", f"{pending:.2f}"),
            ),
        )

        def badges(advance):
            if advance['status'] != SalesInvoiceStatus.FINAL:
                return _pill(SalesInvoiceStatus(advance['status']).label, "#d97706")
            parts = []
            # Deductible once final, paid or not: say when it is not paid yet
            if not advance['paid']:
                parts.append(_pill(_("Unpaid"), "#dc2626"))
            if not advance['deductible']:
                parts.append(_pill(_("not deductible"), "#6b7280"))
            return format_html_join(" ", "{}", ((part,) for part in parts))

        header = format_html(
            '<tr style="font-size: 11px; color: #6b7280; text-align: left;">'
            '<th style="padding: 4px 6px 4px 0; font-weight: 500;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">%</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '<th style="padding: 4px 6px; font-weight: 500; text-align: right;">{}</th>'
            '<th style="padding: 4px 0 4px 6px;"></th>'
            '</tr>',
            _("Advance"), _("Date"), _("Base"), _("Deducted"), _("Pending"),
        )
        rows = format_html_join(
            "",
            '<tr style="border-top: 1px solid #e5e7eb;">'
            '<td style="padding: 4px 6px 4px 0;"><a href="{}">{}</a></td>'
            '<td style="padding: 4px 6px;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right;">{}</td>'
            '<td style="padding: 4px 6px; text-align: right; font-weight: 600;">{}</td>'
            '<td style="padding: 4px 0 4px 6px;">{}</td>'
            '</tr>',
            (
                (
                    reverse("admin:invoices_salesinvoice_change", args=[advance['id']]),
                    advance['invoice_number'],
                    advance['date_issued'].strftime("%d/%m/%Y"),
                    f"{advance['percentage']:.2f}" if advance['percentage'] is not None else "—",
                    f"{advance['base']:.2f}",
                    f"{advance['deducted']:.2f}",
                    f"{advance['pending']:.2f}",
                    badges(advance),
                )
                for advance in advances
            ),
        )

        # No goods left to invoice, so nothing left to deduct the rest from
        # (the expediente shrank): it needs a corrective invoice.
        unsettled = format_html(
            '<div style="margin-top: 6px;">{}</div>',
            _pill(_("Unsettled advance: no goods are left to deduct it from"), "#dc2626"),
        ) if not summary['can_invoice'] and pending > Decimal("0.01") else ""

        return format_html(
            '<div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; max-width: 600px; margin-top: 12px;">{}</div>'
            '<table style="width: 100%; max-width: 760px; margin-top: 8px; font-size: 12px; color: #374151;">{}{}</table>'
            '{}',
            tiles, header, rows, unsettled,
        )

    @admin.display(description=_("Production Hours"))
    def production_hours_summary_display(self, obj):
        """Detail page read-only field: total worked hours, per employee, per session."""
        if not obj.pk:
            return "—"

        summary = obj.production_hours_summary()
        if not summary["employees"]:
            return format_html(
                '<span style="color: #6b7280;">{}</span>',
                _("No completed production sessions yet."),
            )

        total_hours, total_remainder = divmod(summary["total_seconds"], 3600)
        total_minutes = total_remainder // 60

        open_note = format_html(
            '<div style="font-size: 11px; color: #d97706; margin-top: 2px;">{} {}</div>',
            summary["open_sessions"],
            _("session(s) currently in progress (not counted below)"),
        ) if summary["open_sessions"] else ""

        header = format_html(
            '<div style="margin-bottom: 12px;">'
            '<div style="font-size: 11px; color: #6b7280;">{}</div>'
            '<div style="font-size: 20px; font-weight: 700; color: #111827;">{}h {}m</div>'
            '{}'
            '</div>',
            _("Total worked"),
            total_hours,
            total_minutes,
            open_note,
        )

        employee_blocks = []
        for entry in summary["employees"]:
            hours, remainder = divmod(entry["total_seconds"], 3600)
            minutes = remainder // 60

            session_rows = format_html_join(
                "",
                '<tr>'
                '<td style="padding: 2px 6px 2px 0;">{}</td>'
                '<td style="padding: 2px 6px;">{}</td>'
                '<td style="padding: 2px 6px;">{}–{}</td>'
                '<td style="padding: 2px 0 2px 6px; text-align: right;">{}</td>'
                '</tr>',
                (
                    (
                        localtime(session.start_time).strftime("%d/%m/%Y") if session.start_time else "—",
                        session.production_stage,
                        localtime(session.start_time).strftime("%H:%M") if session.start_time else "—",
                        localtime(session.end_time).strftime("%H:%M") if session.end_time else "—",
                        session.formatted_duration,
                    )
                    for session in entry["sessions"]
                ),
            )

            employee_blocks.append(format_html(
                '<div style="border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px;">'
                '<div style="display: flex; justify-content: space-between; font-weight: 600; font-size: 13px; color: #111827;">'
                '<span>{}</span><span>{}h {}m · {} {}</span>'
                '</div>'
                '<table style="width: 100%; margin-top: 6px; font-size: 12px; color: #374151;">{}</table>'
                '</div>',
                entry["employee"].full_name,
                hours,
                minutes,
                len(entry["sessions"]),
                _("session(s)"),
                session_rows,
            ))

        return format_html_join("", "{}", ((part,) for part in [header, *employee_blocks]))
