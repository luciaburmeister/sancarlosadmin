from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from django.utils import timezone
from django.views.generic import TemplateView
from django.core.exceptions import PermissionDenied
from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from management.models import ResourcePlanning, ResourcePlanningType, ResourcePlanningStatus

STEP_MINUTES = 30
PPM = 1.2  # pixels per minute


@dataclass
class UiPlanningBlock:
    id: int
    top_min: int
    height_min: int
    label: str
    color: str
    status: str
    start: datetime
    end: datetime
    project_file_name: str
    planning_type_name: str


class ResourcePlanningScheduleView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Resource Planning"
    permission_required = ("management.view_resourceplanning",)
    template_name = "admin/management/resourceplanning/schedule.html"

    def _grid(self, start_t: time, end_t: time):
        start_dt = datetime.combine(timezone.localdate(), start_t)
        end_dt = datetime.combine(timezone.localdate(), end_t)
        total_min = int((end_dt - start_dt).total_seconds() // 60)

        marks = []
        for m in range(0, total_min + 1, STEP_MINUTES):
            label = (start_dt + timedelta(minutes=m)).strftime("%H:%M")
            marks.append({"minute": m, "label": label, "show": (m % 60 == 0)})
        return marks, total_min

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        # Get companies the user can access (superuser → all)
        user_companies = get_user_companies(user)
        if not user_companies.exists():
            raise PermissionDenied("User has no company")

        companies = list(user_companies.order_by("name"))

        # Company filter — from GET param or default to first company
        company_id = self.request.GET.get("company")
        if company_id:
            try:
                selected_company = user_companies.get(pk=int(company_id))
            except (ValueError, user_companies.model.DoesNotExist):
                selected_company = companies[0]
        else:
            # For non-superusers with one company, auto-select it
            # For superusers, show all unless filtered
            selected_company = companies[0] if len(companies) == 1 else None

        # Parse day filter
        day_str = self.request.GET.get("day")
        if day_str:
            try:
                day = datetime.strptime(day_str, "%Y-%m-%d").date()
            except ValueError:
                day = timezone.localdate()
        else:
            day = timezone.localdate()

        planning_type_id = self.request.GET.get("planning_type")
        status_filter = self.request.GET.get("status")

        start_t, end_t = time(6, 0), time(22, 0)
        start_of_window = timezone.make_aware(datetime.combine(day, start_t))
        end_of_window = timezone.make_aware(datetime.combine(day, end_t))

        # Build queryset
        qs = (
            ResourcePlanning.objects
            .select_related("project_file", "planning_type", "resource_content_type")
            .filter(
                start__lt=end_of_window,
                end__gt=start_of_window,
            )
        )

        # Scope to user's companies
        if selected_company:
            qs = qs.filter(company=selected_company)
        else:
            qs = qs.filter(company__in=user_companies)

        if planning_type_id:
            qs = qs.filter(planning_type_id=planning_type_id)
        if status_filter:
            qs = qs.filter(status=status_filter)

        plannings = list(qs.order_by("start"))

        # Group by resource (content_type + object_id)
        resource_keys = []
        resource_names = {}
        seen = set()

        for p in plannings:
            key = (p.resource_content_type_id, p.resource_object_id)
            if key not in seen:
                seen.add(key)
                resource_keys.append(key)
                resource_names[key] = str(p.resource) if p.resource else f"{p.resource_content_type} #{p.resource_object_id}"

        window_start_naive = timezone.localtime(start_of_window).replace(tzinfo=None)

        blocks_by_resource = {key: [] for key in resource_keys}

        for p in plannings:
            key = (p.resource_content_type_id, p.resource_object_id)
            ds = max(p.start, start_of_window)
            de = min(p.end, end_of_window)

            ds_local = timezone.localtime(ds).replace(tzinfo=None)
            de_local = timezone.localtime(de).replace(tzinfo=None)

            top_min = int((ds_local - window_start_naive).total_seconds() // 60)
            height_min = max(15, int((de_local - ds_local).total_seconds() // 60))

            color = p.planning_type.color if p.planning_type else "#3B82F6"

            blocks_by_resource[key].append(
                UiPlanningBlock(
                    id=p.id,
                    top_min=top_min,
                    height_min=height_min,
                    label=f"{p.project_file.name}" if p.project_file else "—",
                    color=color,
                    status=p.status,
                    start=ds,
                    end=de,
                    project_file_name=str(p.project_file) if p.project_file else "—",
                    planning_type_name=str(p.planning_type) if p.planning_type else "—",
                )
            )

        grid, total_minutes = self._grid(start_t, end_t)
        columns = [
            {
                "resource_name": resource_names[key],
                "blocks": blocks_by_resource.get(key, []),
            }
            for key in resource_keys
        ]

        # Get filter options — planning types across accessible companies
        if selected_company:
            planning_types = list(
                ResourcePlanningType.objects.filter(company=selected_company, is_active=True).order_by("name")
            )
        else:
            planning_types = list(
                ResourcePlanningType.objects.filter(company__in=user_companies, is_active=True).order_by("name")
            )

        statuses = ResourcePlanningStatus.choices

        # Navigation days
        prev_day = day - timedelta(days=1)
        next_day = day + timedelta(days=1)

        ctx.update(
            day=day,
            prev_day=prev_day,
            next_day=next_day,
            grid=grid,
            total_minutes=total_minutes,
            step_minutes=STEP_MINUTES,
            pixels_per_minute=PPM,
            columns=columns,
            planning_types=planning_types,
            selected_planning_type=planning_type_id,
            statuses=statuses,
            selected_status=status_filter,
            has_data=bool(plannings),
            companies=companies,
            selected_company=selected_company,
            show_company_filter=len(companies) > 1,
        )
        return ctx
