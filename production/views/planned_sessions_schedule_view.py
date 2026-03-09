# production/views/planned_sessions_schedule_view.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from django.utils import timezone
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin
from production.models import PlannedProductionSession, ProductionResource
from production.forms.schedule_filters import ScheduleFilterForm
from django.core.exceptions import PermissionDenied

STEP_MINUTES = 15  # grid resolution
PPM = 1.2  # pixels per minute (must match template pixels_per_minute)

@dataclass
class UiSessionBlock:
    id: int
    top_min: int
    height_min: int
    label: str
    start: datetime
    end: datetime


class PlannedSessionsScheduleDayView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Schedule"
    permission_required = ("production.view_plannedproductionsession",)
    template_name = "admin/production/plannedproductionsession/schedule_day.html"

    def _get_company(self):
        employee = getattr(self.request.user, "employee_profile", None)
        if employee and getattr(employee, "company_id", None):
            return employee.company
        # optional fallback if you have user.company
        return getattr(self.request.user, "company", None)

    def _company_hours(self) -> tuple[time, time]:
        """
        Use your company start/end if available. Adjust this to your project:
        - request.user.company
        - request.user.employee_profile.company
        etc.
        """
        company = getattr(self.request.user, "company", None)
        start_t = getattr(company, "start_time", time(8, 0)) if company else time(8, 0)
        end_t = getattr(company, "end_time", time(18, 0)) if company else time(18, 0)
        return start_t, end_t

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
        company = self._get_company()
        if not company:
            raise PermissionDenied("User has no company")
        
        form = ScheduleFilterForm(self.request.GET or None, company=company)

        if form.is_valid():
            day = form.cleaned_data["day"]
            resource_obj = form.cleaned_data["resource_id"]
        else:
            day = timezone.localdate()
            resource_obj = None

        start_t, end_t = self._company_hours()
        start_of_window = timezone.make_aware(datetime.combine(day, start_t))
        end_of_window = timezone.make_aware(datetime.combine(day, end_t))

        # Resources
        resources_qs = ProductionResource.objects.filter(is_active=True).filter(company=company).order_by("id")

        if resource_obj:
            resource_id = resource_obj.pk
            resources_qs = resources_qs.filter(pk=resource_id)
        else:
            resource_id = None

        resources = list(resources_qs)
        resource_ids = [r.id for r in resources]

        # Sessions overlapping the window
        sessions = (
            PlannedProductionSession.objects
            .select_related("production_item", "production_item__ticket", "primary_resource")
            .filter(
                primary_resource_id__in=resource_ids,
                planned_start__lt=end_of_window,
                planned_end__gt=start_of_window,
            )
            .order_by("planned_start")
        )

        # Build blocks per resource
        blocks_by_resource: dict[int, list[UiSessionBlock]] = {r.id: [] for r in resources}
        window_start_local_naive = timezone.localtime(start_of_window).replace(tzinfo=None)

        for s in sessions:
            # Clamp for display
            ds = max(s.planned_start, start_of_window)
            de = min(s.planned_end, end_of_window)

            ds_local = timezone.localtime(ds).replace(tzinfo=None)
            de_local = timezone.localtime(de).replace(tzinfo=None)

            top_min = int((ds_local - window_start_local_naive).total_seconds() // 60)
            height_min = max(1, int((de_local - ds_local).total_seconds() // 60))

            ticket = getattr(s.production_item, "ticket", None)
            ticket_label = f"T{ticket.id}" if ticket else "T?"
            label = f"{ticket_label} · {s.production_stage}"

            blocks_by_resource[s.primary_resource_id].append(
                UiSessionBlock(
                    id=s.id,
                    top_min=top_min,
                    height_min=height_min,
                    label=label,
                    start=ds,
                    end=de,
                )
            )

        grid, total_minutes = self._grid(start_t, end_t)
        columns = [{"resource": r, "blocks": blocks_by_resource.get(r.id, [])} for r in resources]

        ctx.update(
            form=form,
            day=day,
            start_t=start_t,
            end_t=end_t,
            grid=grid,
            total_minutes=total_minutes,
            step_minutes=STEP_MINUTES,
            selected_resource_id=int(resource_id) if resource_id else None,
            all_resources=list(
                ProductionResource.objects.filter(is_active=True, company=company).order_by("id")
            ),
            columns=columns,
            pixels_per_minute=PPM
        )
        return ctx
    