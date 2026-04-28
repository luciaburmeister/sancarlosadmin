import traceback

from django.contrib import admin, messages
from django.http import HttpRequest
from unfold.admin import ModelAdmin, TabularInline,StackedInline
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect, render
from unfold.decorators import display, action
from django.utils import timezone

from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter
)

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
)
from production.services.scheduler.apply_schedule import ScheduleProposalApplier
from production.services.scheduler.factory import build_scheduler_strategy
from production.services.scheduler.scheduler import Scheduler
from production.services.scheduler.serialization import proposal_from_dict, proposal_to_dict
from production.views.configuration_autocomplete import ConfigurationAutocompleteView
from production.views.planned_sessions_schedule_view import PlannedSessionsScheduleDayView
from production.filters.ticket_filters import TicketStageFilter, TicketStageStatusFilter
from production.views.production_item_stages_view import ProductionItemAdvanceStageView
from production.views.production_ticket_stages_view import ProductionTicketStagesView
from production.views.production_control_view import ProductionControlView
from production.views import ProductionTicketHtmlRender
from production.components.status_badge import StatusBadge


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

    list_display = ("id", "company", "source_object", "status", "created_at", "go_to_stages_display")

    list_filter = (
        ("company",AutocompleteSelectMultipleFilter),
        TicketStageFilter,
        TicketStageStatusFilter,
        "status",
        "content_type",
    )

    search_fields = ("object_id",)
    list_select_related = ["company"]
    actions_row = ["view_work_order", "generate_invoice_action"]

    inlines = [ProductionItemInline]
    autocomplete_fields = ("company",)

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
                    ("content_type", "object_id"),
                    "source_object",
                    "project_file",
                    "work_order_pdf",
                    "context",
                )
            },
        ),
    )

    @display(description=_("Stages"))
    def go_to_stages_display(self, obj):
        url = reverse("admin:production_productionticket_stages", args=[obj.pk])
        return format_html(
            '<a href="{}" class="bg-primary-600 font-medium px-3 py-2 rounded-md text-white whitespace-nowrap">{}</a>',
            url,
            _("Go to Stages")
        )

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
        return super().get_queryset(request).prefetch_related("source_object")

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
        "comments",
    )

    readonly_fields = (
        "session_type_display",
        "duration_display",
    )

    ordering = ("-start_time",)

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
        "production_item",
        "production_stage",
        "planned_start",
        "planned_end",
        "primary_resource",
        "locked",
        "source",
    )
    list_filter = ("locked", "source")
    search_fields = ("production_stage",)
    autocomplete_fields = ("production_item", "primary_resource")
    inlines = (PlannedSessionResourceInline,)

    fieldsets = (
        (None, {
            "fields": (
                "production_item",
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

                try:
                    strategy = build_scheduler_strategy(
                        strategy_code=strategy_code,
                        optimization_params={"ts": ts},
                    )
                    scheduler = Scheduler(strategy)
                    proposal = scheduler.schedule(
                        start_date=start_date,
                        end_date=end_date,
                        configurations=configurations,
                    )

                    run.result_payload = proposal_to_dict(proposal)
                    run.solver_status = proposal.solver_status
                    run.objective_value = proposal.objective_value
                    run.total_margin = proposal.total_margin
                    run.status = "solved"
                    run.save(
                        update_fields=[
                            "result_payload",
                            "solver_status",
                            "objective_value",
                            "total_margin",
                            "status",
                        ]
                    )

                    return redirect(
                        "admin:production_schedulingrun_preview",
                        run_id=run.pk,
                    )

                except Exception as exc:
                    run.status = "error"
                    run.error_message = str(exc)
                    run.save(update_fields=["status", "error_message"])
                    traceback.print_exc()
                    messages.error(request, f"Optimization failed: {exc}")
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

        selected_rows = [
            [
                config_id,
                proposal.configuration_margins.get(str(config_id), 0),
            ]
            for config_id in proposal.selected_configuration_ids
        ]

        assignment_rows = [
            [
                a.configuration_name,
                a.item_name,
                a.stage_name,
                a.resource_name,
                a.copy_index,
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
                "headers": [_("Configuration ID"), _("Margin")],
                "rows": selected_rows,
            },
            "assignments_table": {
                "headers": [
                    _("Configuration"),
                    _("Item"),
                    _("Stage"),
                    _("Resource"),
                    _("Copy"),
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