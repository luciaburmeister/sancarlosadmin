
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from django.shortcuts import get_object_or_404

from unfold.views import UnfoldModelAdminViewMixin
from django.views.generic import TemplateView
from django.utils.translation import gettext as _

from unfold.admin import ModelAdmin
from unfold.decorators import display

from production.models import (
    ProductionTicket,
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus
)

from configurator.models import ProductTemplateComponentGroup
from production.components.status_badge import StatusBadge

class ProductionTicketStagesView(UnfoldModelAdminViewMixin, TemplateView):
    title = _("Production stages")
    permission_required = ("production.view_productionticket",)
    template_name = "admin/production/productionticket/planning.html"

    def _status_display(self, status_val: str):
        status_val = (status_val or "").strip()
        color = StatusBadge.STATUS_COLORS.get(status_val, "#6b7280")
        label = _(status_val.replace("_", " ")) if status_val else "—"
        return color, label


    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        ticket = get_object_or_404(ProductionTicket, pk=self.kwargs["object_id"])
        ctx["ticket"] = ticket

        config = ticket.source_object
        product = getattr(config, "product", None)
        stage_order = list(getattr(product, "production_stages", []) or [])

        items = list(ticket.items.select_related("component").order_by("id"))
        employee = getattr(self.request.user, "employee_profile", None)

        ticket_current_stage = None

        for sname in stage_order:
            if any(
                (it.planning or {}).get(sname, {}).get("status") == ProductionItemStatus.IN_PROGRESS
                for it in items
            ):
                ticket_current_stage = sname
                break

        if ticket_current_stage is None:
            for sname in stage_order:
                if any(
                    ((it.planning or {}).get(sname, {}).get("status") or ProductionItemStatus.PENDING)
                    != ProductionItemStatus.DONE
                    for it in items
                ):
                    ticket_current_stage = sname
                    break

        if ticket_current_stage is None and stage_order:
            ticket_current_stage = stage_order[0]

        authorized_stages = stage_order
        if employee:
            if employee.production_stages:
                authorized_stages = [s for s in stage_order if s in employee.production_stages]

        stages_ui = []

        for stage_name in authorized_stages:
            grouped_item_ids = set()

            if product:
                group = (
                    ProductTemplateComponentGroup.objects
                    .filter(product_template=product, stage=stage_name)
                    .prefetch_related("components")
                    .first()
                )
                if group:
                    component_ids = set(group.components.values_list("id", flat=True))
                    grouped_item_ids = {
                        item.id for item in items
                        if item.component_id in component_ids
                    }

            units = []

            if grouped_item_ids:
                unit_items = [item for item in items if item.id in grouped_item_ids]
                unit = self._build_unit(stage_name, unit_items, employee)
                if unit["relevant"]:
                    units.append(unit)

            for item in items:
                if item.id in grouped_item_ids:
                    continue
                unit = self._build_unit(stage_name, [item], employee)
                if unit["relevant"]:
                    units.append(unit)

            if not units:
                continue

            is_current_stage = (stage_name == ticket_current_stage)
            for u in units:
                u["is_current"] = is_current_stage

            stages_ui.append({
                "stage_name": stage_name,
                "units": units,
                "action_url": reverse(
                    "admin:production_productionticket_stage_action",
                    args=[ticket.pk, stage_name],
                ),
            })

        ctx["stages_ui"] = stages_ui
        return ctx

    def _build_unit(self, stage_name, items, employee):
        infos = []

        for item in items:
            planning = item.planning or {}
            info = planning.get(stage_name) or {}
            status = info.get("status") or ProductionItemStatus.PENDING
            infos.append((item, info, status))

        statuses = [status for _, _, status in infos]

        if all(s == ProductionItemStatus.DONE for s in statuses):
            agg_status = ProductionItemStatus.DONE
        elif any(s == ProductionItemStatus.IN_PROGRESS for s in statuses):
            agg_status = ProductionItemStatus.IN_PROGRESS
        else:
            agg_status = ProductionItemStatus.PENDING

        status_color, status_label = self._status_display(agg_status)

        started_at = min(
            (info.get("started_at") for _, info, _ in infos if info.get("started_at")),
            default=None,
        )
        completed_at = max(
            (info.get("completed_at") for _, info, _ in infos if info.get("completed_at")),
            default=None,
        )

        has_active_session = False
        if employee is not None:
            has_active_session = ProductionItemSession.objects.filter(
                production_item__in=[item for item, _, _ in infos],
                production_stage=stage_name,
                created_by=employee,
                status=ProductionItemSessionStatus.IN_PROGRESS,
                end_time__isnull=True,
            ).exists()

        relevant = any(status != ProductionItemStatus.DONE for status in statuses)

        return {
            "items": [
                {
                    "id": item.id,
                    "label": (item.component.name if item.component else (item.data.get("component") or "—")),
                }
                for item, _, _ in infos
            ],
            "item_ids": ",".join(str(item.id) for item, _, _ in infos),
            "status": agg_status,
            "status_color": status_color,
            "status_label": status_label,
            "started_at": started_at,
            "completed_at": completed_at,
            "has_active_session": has_active_session,
            "is_current": False,
            "relevant": relevant,
        }