from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, date, time, timedelta
from urllib.parse import urlencode
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView
from django.core.exceptions import PermissionDenied
from unfold.views import UnfoldModelAdminViewMixin

from django.utils.translation import gettext_lazy as _

from core.admin.company_access import get_user_companies
from management.forms import ResourcePlanningFilterForm, _parse_resource_tokens
from management.models import (
    ProjectFile,
    ResourcePlanning,
    ResourcePlanningStatus,
    ResourcePlanningType,
)


# ponytail: plain class, no dataclass overhead
class CellBlock:
    __slots__ = ("id", "label", "color", "status", "start", "end",
                 "project_file_name", "planning_type_name", "company_id")

    def __init__(self, p, start, end):
        self.id = p.id
        self.company_id = p.company_id
        if p.show_planning_type and p.planning_type:
            self.label = str(p.planning_type)
        else:
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

# Models that can be planned, with their app_label and is_active field
_PLANNABLE_MODELS = [
    ("company", "employee"),
    ("company", "machine"),
    ("assets", "asset"),
]


def _build_day_columns(start_date: date, num_days: int):
    cols = []
    for i in range(num_days):
        d = start_date + timedelta(days=i)
        start_dt = timezone.make_aware(datetime.combine(d, time.min))
        end_dt = timezone.make_aware(datetime.combine(d + timedelta(days=1), time.min))
        cols.append({
            "key": d.isoformat(),
            "date": d.isoformat(),
            "label": d.strftime("%a %d/%m"),
            "start_dt": start_dt,
            "end_dt": end_dt,
        })
    return cols


def _get_active_resources(company_scope, asset_types=None, selected_keys=None):
    """Return all active plannable resources grouped by (ct_id, obj_id) → name, ct_model.

    When `selected_keys` is given, only resources whose (ct_id, obj_id) key is
    in that set are returned — used to restrict the schedule to a hand-picked
    set of employees/machines/assets.
    """
    resources = {}
    for app_label, model_name in _PLANNABLE_MODELS:
        ct = ContentType.objects.get(app_label=app_label, model=model_name)
        Model = ct.model_class()

        qs = Model.objects.filter(is_active=True, company__in=company_scope)

        # Filter assets by asset_type when specified
        if asset_types and model_name == "asset":
            qs = qs.filter(asset_type__in=asset_types)

        for obj in qs:
            key = (ct.id, obj.pk)
            if selected_keys is not None and key not in selected_keys:
                continue
            resources[key] = {
                "name": str(obj),
                "ct_model": model_name,
            }
    return resources


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
        if len(companies) == 1 and "company" not in GET:
            selected_company_ids = [companies[0].pk]
        else:
            selected_company_ids = []
            for company_id in GET.getlist("company"):
                try:
                    selected_company_ids.append(int(company_id))
                except (TypeError, ValueError):
                    continue
        selected_company_ids = [
            company_id for company_id in selected_company_ids
            if user_companies.filter(pk=company_id).exists()
        ]
        company_scope = (
            user_companies.filter(pk__in=selected_company_ids)
            if selected_company_ids else user_companies
        )
        selected_company = company_scope.first() if company_scope.count() == 1 else None

        # ── Filter form (Unfold widgets) ──────────────────────────────
        filter_form = ResourcePlanningFilterForm(
            GET or None,
            company=selected_company,
            user_companies=user_companies,
        )

        # Extract values — use defaults when form not submitted
        if filter_form.is_valid():
            anchor = filter_form.cleaned_data["day"]
            num_days = filter_form.cleaned_data["num_days"]
            selected_companies = []
            for company_id in (filter_form.cleaned_data.get("company") or []):
                try:
                    selected_companies.append(int(company_id))
                except (TypeError, ValueError):
                    continue
            planning_types = filter_form.cleaned_data.get("planning_type")
            status_filters = filter_form.cleaned_data.get("status") or []
            project_file = filter_form.cleaned_data.get("project_file")
            asset_types = filter_form.cleaned_data.get("asset_type")
            resource_pairs = filter_form.cleaned_data.get("resources") or []
            only_with_plans = filter_form.cleaned_data.get("only_with_plans") or False
        else:
            day_raw = GET.get("day", "")
            anchor = timezone.localdate()
            if day_raw:
                try:
                    anchor = date.fromisoformat(day_raw)
                except ValueError:
                    try:
                        anchor = datetime.strptime(day_raw, "%d/%m/%Y").date()
                    except ValueError:
                        anchor = timezone.localdate()

            try:
                num_days = int(GET.get("num_days", 7))
            except (TypeError, ValueError):
                num_days = 7
            num_days = min(max(num_days, 1), 90)

            selected_companies = []
            for company_id in GET.getlist("company"):
                try:
                    selected_companies.append(int(company_id))
                except (TypeError, ValueError):
                    continue

            planning_type_ids = []
            for planning_type_id in GET.getlist("planning_type"):
                try:
                    planning_type_ids.append(int(planning_type_id))
                except (TypeError, ValueError):
                    continue
            planning_types = ResourcePlanningType.objects.filter(pk__in=planning_type_ids)

            status_filters = [
                status for status in GET.getlist("status")
                if status in {choice[0] for choice in ResourcePlanningStatus.choices}
            ]

            project_file = None
            project_file_id = GET.get("project_file")
            if project_file_id:
                try:
                    project_file = ProjectFile.objects.get(pk=int(project_file_id))
                except (ValueError, ProjectFile.DoesNotExist):
                    project_file = None

            asset_type_ids = []
            for asset_type_id in GET.getlist("asset_type"):
                try:
                    asset_type_ids.append(int(asset_type_id))
                except (TypeError, ValueError):
                    continue
            asset_types = asset_type_ids
            resource_pairs = _parse_resource_tokens(GET.get("resources", ""))
            only_with_plans = GET.get("only_with_plans") in {"1", "true", "True", "on"}

        if selected_companies:
            company_scope = user_companies.filter(pk__in=selected_companies)
        selected_company = company_scope.first() if company_scope.count() == 1 else None

        resources_raw = GET.get("resources", "")
        selected_keys = {(ct.id, pk) for ct, pk in resource_pairs} or None

        # ── Build columns ─────────────────────────────────────────────
        columns_meta = _build_day_columns(anchor, num_days)
        window_start = columns_meta[0]["start_dt"]
        window_end = columns_meta[-1]["end_dt"]

        end_date = anchor + timedelta(days=num_days - 1)
        header_label = f"{anchor.strftime('%d %b')} – {end_date.strftime('%d %b %Y')}"

        # ── Nav links (preserve params) ───────────────────────────────
        def _nav_url(day):
            params: dict[str, Any] = {"day": day.isoformat(), "num_days": str(num_days)}
            if selected_companies:
                params["company"] = selected_companies
            if planning_types:
                params["planning_type"] = [str(pt.pk) for pt in planning_types]
            if status_filters:
                params["status"] = status_filters
            if project_file:
                params["project_file"] = str(project_file.pk)
            if asset_types:
                params["asset_type"] = [
                    str(at.pk) if hasattr(at, "pk") else str(at)
                    for at in asset_types
                ]
            if resources_raw:
                params["resources"] = resources_raw
            if only_with_plans:
                params["only_with_plans"] = "on"
            return "?" + urlencode(params, doseq=True)

        prev_url = _nav_url(anchor - timedelta(days=num_days))
        next_url = _nav_url(anchor + timedelta(days=num_days))

        # ── Queryset ──────────────────────────────────────────────────
        qs = (
            ResourcePlanning.objects
            .select_related("project_file", "planning_type", "resource_content_type")
            .filter(start__lt=window_end, end__gt=window_start)
        )
        qs = qs.filter(company__in=company_scope)
        if planning_types:
            qs = qs.filter(planning_type__in=planning_types)
        if status_filters:
            qs = qs.filter(status__in=status_filters)
        if project_file:
            qs = qs.filter(project_file=project_file)
        if asset_types:
            asset_ct = ContentType.objects.get(app_label="assets", model="asset")
            from assets.models import Asset
            asset_ids = Asset.objects.filter(
                asset_type__in=asset_types, is_active=True, company__in=company_scope
            ).values_list("pk", flat=True)
            qs = qs.filter(
                resource_content_type=asset_ct,
                resource_object_id__in=asset_ids,
            )

        plannings = list(qs.order_by("start"))
        if selected_keys is not None:
            plannings = [
                p for p in plannings
                if (p.resource_content_type_id, p.resource_object_id) in selected_keys
            ]

        # ── Seed resource_map with active resources ────────────────────
        # When specific resources are selected, seed exactly those (so an
        # empty-for-the-window resource still shows up as a row). Otherwise
        # seed with ALL active resources, unless filtering by project file —
        # then only show resources with plannings for that specific project.
        resource_map = {}
        if selected_keys is not None or not project_file:
            all_active = _get_active_resources(
                company_scope, asset_types=asset_types, selected_keys=selected_keys,
            )
            for key, info in all_active.items():
                resource_map[key] = {
                    "name": info["name"],
                    "ct_model": info["ct_model"],
                    "ct_id": key[0],
                    "obj_id": key[1],
                    "cells": {col["key"]: [] for col in columns_meta},
                }

        # ── Fill in planning blocks ───────────────────────────────────
        ct_cache = {}
        for p in plannings:
            ct_id = p.resource_content_type_id
            obj_id = p.resource_object_id
            key = (ct_id, obj_id)

            if ct_id not in ct_cache:
                ct_cache[ct_id] = p.resource_content_type.model

            # Resource might not be in all_active (e.g. deactivated but still
            # has plannings in range) — add it on the fly
            if key not in resource_map:
                try:
                    name = str(p.resource) if p.resource else f"{p.resource_content_type} #{obj_id}"
                except Exception:
                    name = f"{p.resource_content_type} #{obj_id}"
                resource_map[key] = {
                    "name": name,
                    "ct_model": ct_cache[ct_id],
                    "ct_id": ct_id,
                    "obj_id": obj_id,
                    "cells": {col["key"]: [] for col in columns_meta},
                }

            for col in columns_meta:
                col_date = date.fromisoformat(col["key"])
                if p.exclude_weekends and col_date.weekday() >= 5:
                    continue
                if p.start < col["end_dt"] and p.end > col["start_dt"]:
                    resource_map[key]["cells"][col["key"]].append(
                        CellBlock(p, max(p.start, col["start_dt"]), min(p.end, col["end_dt"]))
                    )

        if only_with_plans:
            resource_map = {
                key: info
                for key, info in resource_map.items()
                if any(info["cells"][col["key"]] for col in columns_meta)
            }

        # Group by resource type
        type_groups = OrderedDict()
        for (ct_id, obj_id), info in resource_map.items():
            model = info["ct_model"]
            label = RESOURCE_TYPE_LABELS.get(model, model.title())
            if label not in type_groups:
                type_groups[label] = []
            type_groups[label].append({
                "name": info["name"],
                "ct_model": model,
                "ct_id": info["ct_id"],
                "obj_id": info["obj_id"],
                # Each cell: dict with "date_key" + "blocks" so the template can
                # build add-links with the correct ?day= and ?resources= params.
                "cells": [
                    {"date_key": col["key"], "blocks": info["cells"][col["key"]]}
                    for col in columns_meta
                ],
            })

        resource_groups = [
            {"type_label": label, "resources": resources}
            for label, resources in type_groups.items()
        ]

        # ── Filter options for legend ─────────────────────────────────
        pt_qs = ResourcePlanningType.objects.filter(is_active=True)
        pt_qs = pt_qs.filter(company__in=company_scope)
        planning_types = list(pt_qs.order_by("name"))

        # ── Add URL for click-to-create ───────────────────────────────
        add_url = reverse("admin:management_resourceplanning_bulk_add")

        # ── Labels for the resource-picker badges (initial selection) ──
        resource_labels_json = json.dumps({
            f"{info['ct_model']}:{info['obj_id']}": info["name"]
            for info in resource_map.values()
        })

        ctx.update(
            header_label=header_label,
            filter_form=filter_form,
            columns_meta=columns_meta,
            resource_groups=resource_groups,
            resource_labels_json=resource_labels_json,
            has_data=bool(resource_groups),
            planning_types=planning_types,
            companies=companies,
            selected_company=selected_company,
            show_company_filter=len(companies) > 1,
            prev_url=prev_url,
            next_url=next_url,
            add_url=add_url,
            show_cell_create_buttons=not only_with_plans,
        )
        return ctx

