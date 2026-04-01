from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from django.shortcuts import get_object_or_404

from unfold.views import UnfoldModelAdminViewMixin
from django.views.generic import TemplateView
from django.utils.translation import gettext as _

from production.models import (
    ProductionTicket,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus,
)
from configurator.models import ProductTemplateComponentGroup
from production.components.status_badge import StatusBadge
from company.models import Employee


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

        items = list(ticket.items.select_related("component").order_by("id"))
        stage_order = list(getattr(product, "production_stages", []) or [])

        employee = None
        authorized_stages = stage_order

        if not self.request.user.is_superuser:
            employee = (
                Employee.objects.filter(
                    user=self.request.user,
                    company=ticket.company,
                    is_active=True,
                )
                .only("id", "production_stages", "can_work_on_production")
                .first()
            )

            if not employee or not employee.can_work_on_production:
                authorized_stages = []
            else:
                allowed = set(employee.production_stages or [])
                authorized_stages = [s for s in stage_order if s in allowed]

        item_current_stages = {}
        for it in items:
            planning = it.planning or {}
            current_s = None

            for sname in stage_order:
                if sname not in planning:
                    continue

                info = planning.get(sname) or {}
                status = info.get("status") or ProductionItemStatus.PENDING

                if status != ProductionItemStatus.DONE:
                    current_s = sname
                    break

            if not current_s and stage_order:
                current_s = stage_order[-1]

            item_current_stages[it.id] = current_s

        stages_ui = []

        for stage_name in authorized_stages:
            grouped_item_ids = set()

            if product:
                group = (
                    ProductTemplateComponentGroup.objects.filter(
                        product_template=product,
                        stage=stage_name,
                    )
                    .prefetch_related("components")
                    .first()
                )
                if group:
                    component_ids = set(group.components.values_list("id", flat=True))
                    grouped_item_ids = {
                        item.id for item in items if item.component_id in component_ids
                    }

            units = []

            if grouped_item_ids:
                unit_items = [item for item in items if item.id in grouped_item_ids]

                status_groups = {}
                for item in unit_items:
                    planning = item.planning or {}
                    info = planning.get(stage_name) or {}
                    status = info.get("status") or ProductionItemStatus.PENDING
                    status_groups.setdefault(status, []).append(item)

                for status, sub_items in status_groups.items():
                    unit = self._build_unit(stage_name, sub_items, employee, item_current_stages)
                    if unit["relevant"]:
                        units.append(unit)

            for item in items:
                if item.id in grouped_item_ids:
                    continue

                unit = self._build_unit(stage_name, [item], employee, item_current_stages)
                if unit["relevant"]:
                    units.append(unit)

            if not units:
                continue

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

    def _build_unit(self, stage_name, items, employee, item_current_stages):
        infos = []

        for item in items:
            planning = item.planning or {}
            if stage_name not in planning:
                continue

            info = planning.get(stage_name) or {}
            status = info.get("status") or ProductionItemStatus.PENDING
            infos.append((item, info, status))

        if not infos:
            return {"relevant": False}

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
            "is_current": all(item_current_stages.get(item.id) == stage_name for item, _, _ in infos),
            "relevant": relevant,
        }