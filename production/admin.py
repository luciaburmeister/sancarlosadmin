
from datetime import datetime

from django.contrib import admin, messages
from django.http import HttpRequest
from unfold.admin import ModelAdmin, TabularInline,StackedInline
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect, render
from unfold.decorators import display, action
from django.utils import timezone
from production.tasks import run_schedule_optimization
from configurator.models import Configuration
from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
    MultipleChoicesDropdownFilter
)
from django.contrib.contenttypes.prefetch import GenericPrefetch
from production.forms.optimization_form import OptimizeScheduleForm
from production.models import (
    ProductionTicket,
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    PlannedProductionSession,
    PlannedSessionResource,
    ProductionResource,
    OperationRequirement,
    SchedulingRun,
    ProductionTicketStatus,
    AssemblyLine,
    AssemblyLineStatus,
)
from production.filters.ticket_filters import TicketStageFilter, TicketStageStatusFilter, TicketIncompleteStageFilter
from production.services.scheduler import ScheduleProposalApplier, proposal_from_dict
from production.services.scheduler.reasons import describe_reason
from production.views.configuration_autocomplete import ConfigurationAutocompleteView
from production.views.planned_sessions_schedule_view import PlannedSessionsScheduleDayView
from production.views.production_item_stages_view import ProductionItemAdvanceStageView
from production.views.production_ticket_stages_view import ProductionTicketStagesView
from production.views.production_control_view import ProductionControlView
from production.views.assembly_line_view import AssemblyLineActionView
from production.views import ProductionTicketHtmlRender
from production.components.status_badge import StatusBadge

from production.models import Employee, Machine
from unfold.contrib.forms.widgets import ArrayWidget
from core.admin.company_access import get_user_companies

class ProductionItemInline(TabularInline):
    model = ProductionItem
    extra = 0
    show_change_link = True
    tab = True

    fields = (
        "status",
        "planning_display",
        "data",
    )
    readonly_fields = ("planning_display",)

    def get_queryset(self, request):
        """
        get_ordered_planning() falls back to ticket.source_object.product
        when production_stages_order is empty, so eager-load that chain to
        avoid a ticket + GFK + product query per row.
        """
        return (
            super()
            .get_queryset(request)
            .select_related("ticket__content_type")
            .prefetch_related(
                GenericPrefetch(
                    "ticket__source_object",
                    [Configuration.objects.select_related("product")],
                )
            )
        )

    @admin.display(description=_("Production planning"))
    def planning_display(self, obj):
        if not obj.planning:
            return "-"

        STATUS_COLORS = StatusBadge.STATUS_COLORS

        rows = []
        for stage, info in obj.get_ordered_planning():
            status = (info or {}).get("status", "-")
            color = STATUS_COLORS.get(status, "#6b7280")

            rows.append(
                f"""
                <tr>
                    <td style="padding:6px 8px; font-weight:600;">
                        {stage}
                    </td>
                    <td style="padding:6px 8px;">
                        <span style="
                            display:inline-block;
                            padding:2px 8px;
                            border-radius:12px;
                            font-size:12px;
                            background-color: {color} !important;
                            color:white;
                            text-transform:capitalize;
                        ">
                            {_(status.replace("_", " "))}
                        </span>
                    </td>
                </tr>
                """
            )

        return format_html(
            """
            <div style="
                border:1px solid #e5e7eb;
                border-radius:6px;
                overflow:hidden;
                background:#fff;
            ">
                <table style="
                    width:100%;
                    border-collapse:collapse;
                    font-size:13px;
                ">
                    <thead style="background:#f9fafb;">
                        <tr>
                            <th style="padding:6px 8px; text-align:left;">Stage</th>
                            <th style="padding:6px 8px; text-align:left;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {}
                    </tbody>
                </table>
            </div>
            """,
            format_html("".join(rows)),
        )


@admin.register(ProductionTicket)
class ProductionTicketAdmin(CompanyRestrictedAdmin, ModelAdmin):
    change_form_template = "admin/production/productionticket/change_form.html"

    list_display = ("id", "company", "source_object", "show_status_customized_color", "last_stage_label", "created_at", "go_to_stages_display")

    list_filter = (
        TicketIncompleteStageFilter,
        ("company",AutocompleteSelectMultipleFilter),
        ("status",MultipleChoicesDropdownFilter),
        TicketStageFilter,
        TicketStageStatusFilter,
        "last_stage_label",
    )

    search_fields = ("object_id",)
    actions_row = ["view_work_order", "generate_invoice_action"]

    inlines = [ProductionItemInline]
    autocomplete_fields = ("company","project_file")
    list_filter_submit = True

    readonly_fields = (
        "content_type",
        "object_id",
        "source_object",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "company",
                    "status",
                    "last_stage_label",
                    ("content_type", "object_id"),
                    "source_object",
                    "project_file",
                    "work_order_pdf",
                    "context",
                )
            },
        ),
    )

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(ProductionTicketStatus.DONE.label): "success",  # green
            _(ProductionTicketStatus.PENDING.label): "info",  # blue
            _(ProductionTicketStatus.IN_PROGRESS.label): "warning",  # orange
            _(ProductionTicketStatus.CANCELLED.label): "danger",  # red
            
        },
    )
    def show_status_customized_color(self, obj):
        return _(obj.get_status_display())

    @display(description=_("Stages"))
    def go_to_stages_display(self, obj):
        url = reverse("admin:production_productionticket_stages", args=[obj.pk])
        return format_html(
            '<a href="{}" class="bg-primary-600 font-medium px-3 py-2 rounded-md text-white whitespace-nowrap">{}</a>',
            url,
            _("Go to Stages")
        )

    @display(description=_("First incomplete stage"))
    def first_incomplete_stage(self, obj):
        return obj.get_first_incomplete_stage() or "—"

    @action(
        description=_("See Work Order"),
        url_path="production-productionticketadmin-view-order",
        attrs={"target": "_blank"}
    )
    def view_work_order(self, request: HttpRequest, object_id: int):
        return redirect(
            reverse("admin:production_productionticket_order_html", args=[object_id])
        )

    @action(
        description=_("Generate Invoice"),
        url_path="generate-invoice",
    )
    def generate_invoice_action(self, request, object_id):
        from invoices.services.sales_invoice_service import SalesInvoiceService
        from invoices.models import SalesInvoice
        from production.models import ProductionTicket
        
        ticket = ProductionTicket.objects.get(pk=object_id)
        if ticket.content_type.model == 'configuration':
            from configurator.models import Configuration
            config = Configuration.objects.get(pk=ticket.object_id)
            
            existing = SalesInvoice.objects.filter(configuration=config).first()
            if existing:
                self.message_user(request, _("An invoice already exists for this configuration."), level="warning")
                return redirect(reverse("admin:invoices_salesinvoice_change", args=[existing.id]))

            invoice = SalesInvoiceService.create_from_configuration(config)
            self.message_user(request, _("Invoice {} generated successfully.").format(invoice.invoice_number))
            return redirect(reverse("admin:invoices_salesinvoice_change", args=[invoice.id]))
        else:
            self.message_user(request, _("This ticket is not linked to a configuration."), level="error")
            return redirect(request.META.get('HTTP_REFERER'))

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["production_control_url"] = reverse("admin:production_control")
        return super().changelist_view(request, extra_context=extra_context)
    
    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("company", "content_type", "project_file")
            .prefetch_related(
                GenericPrefetch(
                    "source_object",
                    [Configuration.objects.select_related("product")],
                )
            )
        )

    def get_urls(self):
        urls = super().get_urls()

        custom = [
            path(
                "control/",
                self.admin_site.admin_view(
                    ProductionControlView.as_view(model_admin=self)
                ),
                name="production_control",
            ),
            path(
                "<int:object_id>/stages/",
                self.admin_site.admin_view(
                    ProductionTicketStagesView.as_view(model_admin=self)
                ),
                name="production_productionticket_stages",
            ),
            path(
                "<int:ticket_id>/stages/<str:stage_name>/action/",
                self.admin_site.admin_view(
                    ProductionItemAdvanceStageView.as_view(model_admin=self)
                ),
                name="production_productionticket_stage_action",
            ),
            path(
                "<int:ticket_id>/order_html/",
                self.admin_site.admin_view(
                    ProductionTicketHtmlRender.as_view(model_admin=self)
                ),
                name="production_productionticket_order_html",
            ),
        ]
        return custom + urls


class ProductionItemSessionInline(StackedInline):
    model = ProductionItemSession
    extra = 0
    show_change_link = True
    tab = True

    fields = (
        "session_type_display",
        "created_by",
        "completed_by",
        "production_stage",
        "status",
        "start_time",
        "end_time",
        "duration_display",
        "comments_display",
    )

    readonly_fields = (
        "session_type_display",
        "duration_display",
        "comments_display",
    )

    ordering = ("-start_time",)

    def comments_display(self, obj):
        lines = []
        for entry in obj.comment_entries or []:
            if not isinstance(entry, dict):
                continue
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            at = entry.get("at")
            if at:
                try:
                    at_dt = timezone.localtime(datetime.fromisoformat(at))
                    lines.append(f"[{at_dt.strftime('%d/%m/%Y %H:%M')}] {text}")
                    continue
                except ValueError:
                    pass
            lines.append(text)
        return "\n".join(lines) or "—"

    comments_display.short_description = _("Comments")

    def duration_display(self, obj):
        if not obj.duration:
            return "—"
        total_seconds = int(obj.duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m"

    duration_display.short_description = _("Duration")

    @display(
        description=_("Type"),
        label={
            "activityfinished": "success",
            "session_finished": "info",
            "working": "warning",
        },
    )
    def session_type_display(self, obj):
        if obj.kind:
            return obj.get_kind_display()

        if obj.status == ProductionItemSessionStatus.IN_PROGRESS:
            return "working"

        return None


class OperationRequirementInline(TabularInline):
    model = OperationRequirement
    extra = 0


@admin.register(ProductionItem)
class ProductionItemAdmin(CompanyRestrictedAdmin, ModelAdmin):
    actions_list = ["kanban_view_action"]
    list_display = (
        "id",
        "company",
        "ticket",
        "status",
        "assigned_employees_display",
        "sessions_count",
        "created_at",
    )

    list_filter = (("company",AutocompleteSelectMultipleFilter), "status")
    search_fields = ("id", "ticket__id")

    readonly_fields = (
        "component",
        "component_id",
        "quantity",
        "length",
        "data",
        "planning",
    )

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "company",
                    "ticket",
                    "status",
                    ("component", "component_id"),
                    ("quantity", "length"),
                    "assigned_employees",
                    "data",
                    "planning",
                )
            },
        ),
    )

    inlines = [ProductionItemSessionInline,OperationRequirementInline]
    filter_horizontal = ("assigned_employees",)

    def get_urls(self):
        from production.views.kanban_view import ProductionItemKanbanView
        urls = super().get_urls()
        custom_urls = [
            path("kanban/", self.admin_site.admin_view(ProductionItemKanbanView.as_view(model_admin=self)), name="production_productionitem_kanban"),
        ]
        return custom_urls + urls

    @action(description=_("Kanban View"), url_path="kanban-view", icon="view_kanban")
    def kanban_view_action(self, request: HttpRequest):
        return redirect(
            reverse("admin:production_productionitem_kanban")
        )

    def has_kanban_view_action_permission(self, request: HttpRequest):
        return True

    def assigned_employees_display(self, obj):
        return ", ".join([str(e) for e in obj.assigned_employees.all()])
    assigned_employees_display.short_description = _("Assigned Employees")

    def sessions_count(self, obj):
        return obj.sessions.count()

    sessions_count.short_description = _("Sessions")


@admin.register(ProductionItemSession)
class ProductionItemSessionAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "id",
        "company",
        "production_item",
        "production_stage",
        "show_status_colored",
        "start_time",
        "end_time",
    )

    list_filter = (
        "company",
        "status",
        "production_stage",
    )

    search_fields = (
        "production_stage",
        "created_by__name",
        "completed_by__name",
        "production_item__id",
    )

    autocomplete_fields = (
        "production_item",
        "created_by",
        "completed_by",
    )

    actions = (
        "start_sessions",
        "finish_sessions",
    )

    ordering = ("-created_at",)

    @display(
        description=_("Status"),
        ordering="status",
        label={
            ProductionItemSessionStatus.DONE: "success",
            ProductionItemSessionStatus.PENDING: "info",
            ProductionItemSessionStatus.IN_PROGRESS: "warning",
            ProductionItemSessionStatus.CANCELLED: "danger",
        },
    )
    def show_status_colored(self, obj):
        return obj.status


@admin.register(ProductionResource)
class ProductionResourceAdmin(ModelAdmin):
    list_display = ("id", "target", "daily_capacity_minutes", "is_active")
    list_filter = ("is_active",)
    search_fields = ("object_id",)
    readonly_fields = ("target",)
    autocomplete_fields = ("company",)

    fieldsets = (
        (None, {
            "fields": (
                ("content_type", "object_id"),
                "daily_capacity_minutes",
                ("company", "is_active"),
            )
        }),
    )

    def target(self, obj):
        return obj.target


class PlannedSessionResourceInline(TabularInline):
    model = PlannedSessionResource
    extra = 0
    autocomplete_fields = ("resource",)


@admin.register(PlannedProductionSession)
class PlannedProductionSessionAdmin(ModelAdmin):
    list_display = (
        "id",
        "production_ticket",
        "production_stage",
        "planned_start",
        "planned_end",
        "primary_resource",
        "locked",
        "source",
    )
    list_filter = ("locked", "source")
    search_fields = ("production_stage",)
    autocomplete_fields = ("production_ticket", "primary_resource")
    list_select_related = (
        "production_ticket",
        "primary_resource__content_type"
    )
    inlines = (PlannedSessionResourceInline,)

    fieldsets = (
        (None, {
            "fields": (
                "production_ticket",
                "production_stage",
            )
        }),
        ("Schedule", {
            "fields": (
                ("planned_start", "planned_end"),
                "primary_resource",
            )
        }),
        ("Controls", {
            "fields": (
                "locked",
                "source",
                "notes",
            )
        }),
    )
    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related(
                GenericPrefetch(
                    "primary_resource__target",
                    [Employee.objects.all(), Machine.objects.all()],
                ),
            )
        )

    def get_urls(self):
        urls = super().get_urls()

        schedule_view = self.admin_site.admin_view(
            PlannedSessionsScheduleDayView.as_view(model_admin=self)
        )

        custom = [
            path(
                "schedule/",
                schedule_view,
                name="production_plannedproductionsession_schedule",
            ),
        ]
        return custom + urls
    

@admin.register(SchedulingRun)
class SchedulingRunAdmin(ModelAdmin):
    change_list_template = "admin/production/schedulingrun/change_list.html"
    list_fullwidth = True

    list_display = (
        "id",
        "created_at",
        "created_by",
        "strategy",
        "start_date",
        "end_date",
        "slot_minutes",
        "status",
        "solver_status",
        "total_margin",
        "applied_at",
    )
    readonly_fields = (
        "created_at",
        "created_by",
        "input_payload",
        "result_payload",
        "solver_status",
        "objective_value",
        "total_margin",
        "applied_at",
        "discarded_at",
        "error_message",
    )
    actions_row = ["preview_schedule_action"]

    @action(
        description=_("Preview Schedule"),
        url_path="preview-schedule",
    )
    def preview_schedule_action(self, request: HttpRequest, object_id: int):
        run = get_object_or_404(SchedulingRun, pk=object_id)
        if run.status != "solved":
            messages.warning(request, _("This run has not been solved yet."))
            return redirect(reverse("admin:production_schedulingrun_change", args=[object_id]))
        return redirect(reverse("admin:production_schedulingrun_preview", args=[object_id]))

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "configuration-autocomplete/",
                self.admin_site.admin_view(ConfigurationAutocompleteView.as_view(model_admin=self)),
                name="production_configuration_autocomplete",
            ),
            path(
                "optimize/",
                self.admin_site.admin_view(self.optimize_view),
                name="production_schedulingrun_optimize",
            ),
            path(
                "<int:run_id>/preview/",
                self.admin_site.admin_view(self.preview_view),
                name="production_schedulingrun_preview",
            ),
            path(
                "<int:run_id>/apply/",
                self.admin_site.admin_view(self.apply_view),
                name="production_schedulingrun_apply",
            ),
            path(
                "<int:run_id>/discard/",
                self.admin_site.admin_view(self.discard_view),
                name="production_schedulingrun_discard",
            ),
        ]
        return custom_urls + urls

    def optimize_view(self, request):
        if request.method == "POST":
            form = OptimizeScheduleForm(request=request, data=request.POST)
            if form.is_valid():
                strategy_code = form.cleaned_data["strategy"]
                start_date = form.cleaned_data["start_date"]
                end_date = form.cleaned_data["end_date"]
                ts = form.cleaned_data["ts"]
                configurations = list(form.cleaned_data["configurations"])

                run = SchedulingRun.objects.create(
                    created_by=request.user,
                    strategy=strategy_code,
                    start_date=start_date,
                    end_date=end_date,
                    slot_minutes=ts,
                    status="pending",
                    input_payload={
                        "configuration_ids": [c.pk for c in configurations],
                    },
                )

                run_schedule_optimization.delay(run.pk)

                messages.success(
                    request,
                    _("Optimization started in the background. Refresh this page to check progress."),
                )
                return redirect("admin:production_schedulingrun_change", run.pk)
        else:
            form = OptimizeScheduleForm(request=request)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Optimize production schedule",
            "form": form,
            "form_media": form.media,
            "cancel_url": reverse("admin:production_schedulingrun_changelist"),
            "breadcrumbs": [
                {"title": _("Scheduling runs"), "link": reverse("admin:production_schedulingrun_changelist")},
                {"title": _("Optimize schedule")},
            ],
        }
        return render(request, "admin/production/schedulingrun/optimize.html", context)

    def preview_view(self, request, run_id: int):
        run = get_object_or_404(SchedulingRun, pk=run_id)
        proposal = proposal_from_dict(run.result_payload)

        configuration_names = {
            config.pk: str(config)
            for config in Configuration.objects.filter(
                pk__in=(
                    proposal.selected_configuration_ids
                    + proposal.unselected_configuration_ids
                )
            )
        }

        selected_rows = [
            [
                configuration_names.get(config_id, config_id),
                proposal.configuration_margins.get(str(config_id), 0),
            ]
            for config_id in proposal.selected_configuration_ids
        ]

        unselected_rows = []
        for config_id in proposal.unselected_configuration_ids:
            reasons = proposal.unselected_reasons.get(config_id, [])
            if reasons:
                reasons_cell = format_html_join(
                    mark_safe("<br>"), "{}",
                    ((describe_reason(reason),) for reason in reasons),
                )
            else:
                reasons_cell = _("No reason recorded (run predates this feature).")
            unselected_rows.append([
                configuration_names.get(config_id, config_id),
                proposal.configuration_margins.get(str(config_id), 0),
                reasons_cell,
            ])

        assignment_rows = [
            [
                a.configuration_name,
                a.stage_name,
                a.resource_name,
                timezone.localtime(a.start) if timezone.is_aware(a.start) else a.start,
                timezone.localtime(a.end) if timezone.is_aware(a.end) else a.end,
                a.minutes,
            ]
            for a in proposal.assignments
        ]

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Scheduling run #{run.pk} preview",
            "run": run,
            "cancel_url": reverse("admin:production_schedulingrun_changelist"),
            "proposal": proposal,
            "breadcrumbs": [
                {"title": _("Scheduling runs"), "link": reverse("admin:production_schedulingrun_changelist")},
                {"title": _("Preview")},
            ],
            "summary_cards": [
                {"title": _("Solver status"), "metric": proposal.solver_status},
                {"title": _("Optimal"), "metric": _("Yes") if proposal.is_optimal else _("No")},
                {"title": _("Total margin"), "metric": proposal.total_margin},
                {"title": _("Assignments"), "metric": len(proposal.assignments)},
            ],
            "selected_configs_table": {
                "headers": [_("Configuration"), _("Margin")],
                "rows": selected_rows,
            },
            "unselected_configs_table": {
                "headers": [_("Configuration"), _("Margin"), _("Why it was not planned")],
                "rows": unselected_rows,
            },
            "assignments_table": {
                "headers": [
                    _("Configuration"),
                    _("Stage"),
                    _("Resource"),
                    _("Start"),
                    _("End"),
                    _("Minutes"),
                ],
                "rows": assignment_rows,
            },
        }
        return render(request, "admin/production/schedulingrun/preview.html", context)

    def apply_view(self, request, run_id: int):
        run = get_object_or_404(SchedulingRun, pk=run_id)

        if request.method != "POST":
            return redirect("admin:production_schedulingrun_preview", run_id=run.pk)

        if run.status != "solved":
            messages.error(request, "Only solved runs can be applied.")
            return redirect("admin:production_schedulingrun_preview", run_id=run.pk)

        proposal = proposal_from_dict(run.result_payload)

        applier = ScheduleProposalApplier()
        created = applier.apply(proposal)

        run.status = "applied"
        run.applied_at = timezone.now()
        run.save(update_fields=["status", "applied_at"])

        messages.success(
            request,
            f"Applied schedule successfully. Created {len(created)} planned production sessions.",
        )
        return redirect("admin:production_plannedproductionsession_changelist")

    def discard_view(self, request, run_id: int):
        run = get_object_or_404(SchedulingRun, pk=run_id)

        if request.method == "POST" and run.status in {"pending", "solved"}:
            run.status = "discarded"
            run.discarded_at = timezone.now()
            run.save(update_fields=["status", "discarded_at"])
            messages.success(request, "Scheduling proposal discarded.")

        return redirect("admin:production_schedulingrun_changelist")

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["optimize_url"] = reverse("admin:production_schedulingrun_optimize")
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(AssemblyLine)
class AssemblyLineAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = (
        "id",
        "company",
        "show_stages_display",
        "show_status_colored",
        "ticket_count_display",
        "started_by",
        "started_at",
        "finished_at",
    )

    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("status", MultipleChoicesDropdownFilter),
    )

    search_fields = ("stages",)
    list_select_related = ["company", "started_by"]
    autocomplete_fields = ("company", "started_by")
    filter_horizontal = ("tickets",)
    list_filter_submit = True

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "company",
                    "stages",
                    ("status", "parallel_stages"),
                    "tickets",
                )
            },
        ),
        (
            _("Execution"),
            {
                "fields": (
                    "started_by",
                    "started_at",
                    "finished_at",
                ),
            },
        ),
    )

    readonly_fields = ("started_at", "finished_at")

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)

        if obj and obj.company:
            stages = obj.company.production_stages or []
        else:
            stages = []
            for company in get_user_companies(request.user):
                stages.extend(company.production_stages or [])
            stages = list(dict.fromkeys(stages))

        form.base_fields["stages"].widget = ArrayWidget(
            choices=[(s, s) for s in stages]
        )
        return form

    @display(description=_("Stages"))
    def show_stages_display(self, obj):
        return ", ".join(obj.stages) if obj.stages else "—"

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(AssemblyLineStatus.DRAFT.label): "info",
            _(AssemblyLineStatus.READY.label): "info",
            _(AssemblyLineStatus.IN_PROGRESS.label): "warning",
            _(AssemblyLineStatus.PAUSED.label): "warning",
            _(AssemblyLineStatus.DONE.label): "success",
        },
    )
    def show_status_colored(self, obj):
        return _(obj.get_status_display())

    @display(description=_("Tickets"))
    def ticket_count_display(self, obj):
        return obj.tickets.count()


    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:assembly_line_id>/action/",
                self.admin_site.admin_view(
                    AssemblyLineActionView.as_view(model_admin=self)
                ),
                name="production_assemblyline_action",
            ),
        ]
        return custom + urls