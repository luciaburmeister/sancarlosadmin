from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, date, time, timedelta
from typing import Any

from django.utils import timezone
from django.views.generic import TemplateView
from django.core.exceptions import PermissionDenied
from unfold.views import UnfoldModelAdminViewMixin

from django.utils.translation import gettext_lazy as _

from core.admin.company_access import get_user_companies
from management.forms import ResourcePlanningFilterForm
from management.models import ResourcePlanning, ResourcePlanningType, ResourcePlanningStatus


# ponytail: plain class, no dataclass overhead
class CellBlock:
    __slots__ = ("id", "label", "color", "status", "start", "end",
                 "project_file_name", "planning_type_name")

    def __init__(self, p, start, end):
        self.id = p.id
        self.label = p.project_file.name if p.project_file else "—"
        self.color = p.planning_type.color if p.planning_type else "#3B82F6"
        self.status = p.status
        self.start = start
        self.end = end
        self.project_file_name = str(p.project_file) if p.project_file else "—"
        self.planning_type_name = str(p.planning_type) if p.planning_type else "—"


RESOURCE_TYPE_LABELS = {
    "employee": _("Employees"),
    "machine": _("Machines"),
    "asset": _("Assets"),
}


def _build_day_columns(start_date: date, num_days: int):
    cols = []
    for i in range(num_days):
        d = start_date + timedelta(days=i)
        start_dt = timezone.make_aware(datetime.combine(d, time.min))
        end_dt = timezone.make_aware(datetime.combine(d + timedelta(days=1), time.min))
        cols.append({
            "key": d.isoformat(),
            "label": d.strftime("%a %d/%m"),
            "start_dt": start_dt,
            "end_dt": end_dt,
        })
    return cols


class ResourcePlanningScheduleView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Resource Planning"
    permission_required = ("management.view_resourceplanning",)
    template_name = "admin/management/resourceplanning/schedule.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        GET = self.request.GET

        # ── Company scoping ───────────────────────────────────────────
        user_companies = get_user_companies(user)
        if not user_companies.exists():
            raise PermissionDenied("User has no company")

        companies = list(user_companies.order_by("name"))
        company_id = GET.get("company")
        if company_id:
            try:
                selected_company = user_companies.get(pk=int(company_id))
            except (ValueError, user_companies.model.DoesNotExist):
                selected_company = companies[0]
        else:
            selected_company = companies[0] if len(companies) == 1 else None

        # ── Filter form (Unfold widgets) ──────────────────────────────
        filter_form = ResourcePlanningFilterForm(
            GET or None,
            company=selected_company,
            user_companies=user_companies if not selected_company else None,
        )

        # Extract values — use defaults when form not submitted
        if filter_form.is_valid():
            anchor = filter_form.cleaned_data["day"]
            num_days = filter_form.cleaned_data["num_days"]
            planning_type = filter_form.cleaned_data.get("planning_type")
            status_filter = filter_form.cleaned_data.get("status")
        else:
            anchor = timezone.localdate()
            num_days = 7
            planning_type = None
            status_filter = None

        # ── Build columns ─────────────────────────────────────────────
        columns_meta = _build_day_columns(anchor, num_days)
        window_start = columns_meta[0]["start_dt"]
        window_end = columns_meta[-1]["end_dt"]

        end_date = anchor + timedelta(days=num_days - 1)
        header_label = f"{anchor.strftime('%d %b')} – {end_date.strftime('%d %b %Y')}"

        # ── Nav links (preserve params) ───────────────────────────────
        def _nav_url(day):
            params = {"day": day.isoformat(), "num_days": str(num_days)}
            if selected_company:
                params["company"] = str(selected_company.id)
            if planning_type:
                params["planning_type"] = str(planning_type.pk)
            if status_filter:
                params["status"] = status_filter
            return "?" + "&".join(f"{k}={v}" for k, v in params.items())

        prev_url = _nav_url(anchor - timedelta(days=num_days))
        next_url = _nav_url(anchor + timedelta(days=num_days))

        # ── Queryset ──────────────────────────────────────────────────
        qs = (
            ResourcePlanning.objects
            .select_related("project_file", "planning_type", "resource_content_type")
            .filter(start__lt=window_end, end__gt=window_start)
        )
        if selected_company:
            qs = qs.filter(company=selected_company)
        else:
            qs = qs.filter(company__in=user_companies)
        if planning_type:
            qs = qs.filter(planning_type=planning_type)
        if status_filter:
            qs = qs.filter(status=status_filter)

        plannings = list(qs.order_by("start"))

        # ── Group resources by content type ───────────────────────────
        resource_map = {}
        ct_cache = {}

        for p in plannings:
            ct_id = p.resource_content_type_id
            obj_id = p.resource_object_id
            key = (ct_id, obj_id)

            if ct_id not in ct_cache:
                ct_cache[ct_id] = p.resource_content_type.model

            if key not in resource_map:
                try:
                    name = str(p.resource) if p.resource else f"{p.resource_content_type} #{obj_id}"
                except Exception:
                    name = f"{p.resource_content_type} #{obj_id}"
                resource_map[key] = {
                    "name": name,
                    "ct_model": ct_cache[ct_id],
                    "cells": {col["key"]: [] for col in columns_meta},
                }

            for col in columns_meta:
                if p.start < col["end_dt"] and p.end > col["start_dt"]:
                    resource_map[key]["cells"][col["key"]].append(
                        CellBlock(p, max(p.start, col["start_dt"]), min(p.end, col["end_dt"]))
                    )

        # Group by resource type
        type_groups = OrderedDict()
        for (ct_id, obj_id), info in resource_map.items():
            model = info["ct_model"]
            label = RESOURCE_TYPE_LABELS.get(model, model.title())
            if label not in type_groups:
                type_groups[label] = []
            type_groups[label].append({
                "name": info["name"],
                "cells": [info["cells"][col["key"]] for col in columns_meta],
            })

        resource_groups = [
            {"type_label": label, "resources": resources}
            for label, resources in type_groups.items()
        ]

        # ── Filter options for legend ─────────────────────────────────
        pt_qs = ResourcePlanningType.objects.filter(is_active=True)
        if selected_company:
            pt_qs = pt_qs.filter(company=selected_company)
        else:
            pt_qs = pt_qs.filter(company__in=user_companies)
        planning_types = list(pt_qs.order_by("name"))

        ctx.update(
            header_label=header_label,
            filter_form=filter_form,
            columns_meta=columns_meta,
            resource_groups=resource_groups,
            has_data=bool(plannings),
            planning_types=planning_types,
            companies=companies,
            selected_company=selected_company,
            show_company_filter=len(companies) > 1,
            prev_url=prev_url,
            next_url=next_url,
        )
        return ctx
