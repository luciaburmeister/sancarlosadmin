import json
from datetime import date, time, timedelta
from urllib.parse import urlencode

from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from company.models import Employee
from core.admin.company_access import get_user_companies
from assets.forms import AssetScheduleFilterForm
from assets.models import Asset, AssetPlanning, SERVICE_TYPE_COLORS, ServiceType
from assets.services.auto_assigner import URGENCY_ORDER, auto_link_transport
from assets.services.route_optimizer import GoogleRouteOptimizer

DEFAULT_BLOCK_MINUTES = 60
FILTER_QUERY_KEYS = frozenset({
    "assigned_to",
    "transport_asset",
    "service_type",
    "status",
    "urgency",
    "planning_type",
    "only_with_plans",
})


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _minutes_to_time(minutes) -> time:
    minutes = max(0, min(int(minutes), 24 * 60 - 1))
    return time(minutes // 60, minutes % 60)


def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _zone_info(zone):
    if not zone:
        return {"name": "", "address": ""}
    return {"name": zone.name, "address": zone.full_address}


def _planning_card_data(planning: AssetPlanning) -> dict:
    """Shared display fields for grid blocks and backlog cards."""
    origin = _zone_info(planning.origin)
    destination = _zone_info(planning.destination)
    service_type_label = planning.get_service_type_display() if planning.service_type else ""
    request_changes_pending = planning.has_pending_request_changes
    is_old_plan = planning.is_old_plan
    request_url = ""
    if request_changes_pending:
        try:
            request_url = reverse(
                "admin:assets_assetplanningrequest_change",
                args=[planning.source_request.pk],
            )
        except Exception:
            request_url = ""
    old_plan_label = ""
    if planning.is_superseded:
        old_plan_label = str(_("Old plan"))
    elif request_changes_pending:
        old_plan_label = str(_("Old plan — request changed"))
    return {
        "label": service_type_label or planning.get_planning_type_display(),
        "sublabel": destination["name"] or origin["name"],
        "asset": planning.asset.name if planning.asset_id else "",
        "origin_name": origin["name"],
        "origin_address": origin["address"],
        "destination_name": destination["name"],
        "destination_address": destination["address"],
        "truck": planning.transport_asset.name if planning.transport_asset_id else "",
        "urgency": planning.urgency,
        "urgency_label": planning.get_urgency_display(),
        "planning_type": planning.planning_type,
        "planning_type_label": planning.get_planning_type_display(),
        "service_type": planning.service_type or "",
        "service_type_label": service_type_label,
        "cuba_size": planning.cuba_size or "",
        "cuba_size_label": planning.get_cuba_size_display() if planning.cuba_size else "",
        "cuba_operation": planning.cuba_operation_type or "",
        "cuba_operation_label": (
            planning.get_cuba_operation_type_display() if planning.cuba_operation_type else ""
        ),
        "load_description": (planning.load_description or "").strip(),
        "notes": (planning.notes or "").strip(),
        "status": planning.status,
        "status_label": planning.get_status_display(),
        "color": planning.service_type_color,
        "urgency_color": planning.urgency_color,
        "request_changes_pending": request_changes_pending,
        "is_old_plan": is_old_plan,
        "is_superseded": planning.is_superseded,
        "old_plan_label": old_plan_label,
        "request_url": request_url,
    }


def _filter_query_items(params, exclude=frozenset({"week"})):
    items = []
    for key in params:
        if key in exclude or key not in FILTER_QUERY_KEYS:
            continue
        for value in params.getlist(key):
            items.append((key, value))
    return items


def _apply_planning_filters(qs, filters: dict):
    assigned_to = filters.get("assigned_to") or []
    transport_asset = filters.get("transport_asset") or []
    service_types = filters.get("service_type") or []
    statuses = filters.get("status") or []
    urgencies = filters.get("urgency") or []
    planning_types = filters.get("planning_type") or []

    if assigned_to:
        qs = qs.filter(assigned_to__in=assigned_to)
    if transport_asset:
        qs = qs.filter(transport_asset__in=transport_asset)
    if service_types:
        qs = qs.filter(service_type__in=service_types)
    if statuses:
        qs = qs.filter(status__in=statuses)
    else:
        # Hide cancelled, but keep superseded "old plans" so reviewers can see what was replaced.
        qs = qs.exclude(status="cancelled", superseded_by__isnull=True)
    if urgencies:
        qs = qs.filter(urgency__in=urgencies)
    if planning_types:
        qs = qs.filter(planning_type__in=planning_types)
    return qs


class _CompanyWeekMixin:
    """Shared company/week resolution for the two schedule views below."""

    def _company(self, request):
        employee = Employee.objects.filter(user=request.user).select_related("company").first()
        if employee and employee.company_id:
            return employee.company
        return get_user_companies(request.user).order_by("name").first()

    def _week_start_param(self, request):
        raw = request.GET.get("week")
        if raw:
            try:
                return _week_start(date.fromisoformat(raw))
            except ValueError:
                pass
        return _week_start(timezone.localdate())


class AssetScheduleView(_CompanyWeekMixin, UnfoldModelAdminViewMixin, TemplateView):
    """
    Weekly staffing calendar: one row per active employee, plannings as
    draggable blocks (move/resize/unassign). Unlike ResourcePlanning's
    schedule (management/views/resource_planning_schedule.py), the "Backlog"
    panel here is drag-and-droppable straight onto the grid. Editing fields
    the grid doesn't expose, and creating new plannings, both go through the
    standard admin form in a new tab — asset/route need the real autocomplete
    widgets, which a custom quick-create can't offer.
    """
    template_name = "admin/assets/asset/schedule.html"
    permission_required = ("assets.view_asset",)
    title = _("Schedule")

    def _parse_filters(self, request, company):
        filter_form = AssetScheduleFilterForm(request.GET or None, company=company)
        if filter_form.is_valid():
            return filter_form, {
                "assigned_to": list(filter_form.cleaned_data.get("assigned_to") or []),
                "transport_asset": list(filter_form.cleaned_data.get("transport_asset") or []),
                "service_type": list(filter_form.cleaned_data.get("service_type") or []),
                "status": list(filter_form.cleaned_data.get("status") or []),
                "urgency": list(filter_form.cleaned_data.get("urgency") or []),
                "planning_type": list(filter_form.cleaned_data.get("planning_type") or []),
                "only_with_plans": bool(filter_form.cleaned_data.get("only_with_plans")),
            }

        assigned_ids, truck_ids = [], []
        for raw in request.GET.getlist("assigned_to"):
            try:
                assigned_ids.append(int(raw))
            except (TypeError, ValueError):
                pass
        for raw in request.GET.getlist("transport_asset"):
            try:
                truck_ids.append(int(raw))
            except (TypeError, ValueError):
                pass
        employees = list(
            Employee.objects.filter(company=company, pk__in=assigned_ids)
        ) if company and assigned_ids else []
        trucks = list(
            Asset.objects.filter(company=company, pk__in=truck_ids)
        ) if company and truck_ids else []
        return filter_form, {
            "assigned_to": employees,
            "transport_asset": trucks,
            "service_type": request.GET.getlist("service_type"),
            "status": request.GET.getlist("status"),
            "urgency": request.GET.getlist("urgency"),
            "planning_type": request.GET.getlist("planning_type"),
            "only_with_plans": request.GET.get("only_with_plans") in {"1", "true", "True", "on"},
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        can_edit = request.user.has_perm("assets.change_assetplanning")
        company = self._company(request)
        week_start = self._week_start_param(request)
        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        filter_form, filters = self._parse_filters(request, company)

        employees = list(
            Employee.objects.filter(company=company, is_active=True).order_by(
                "first_name", "last_name"
            )
        ) if company else []
        if filters["assigned_to"]:
            selected_ids = {e.pk for e in filters["assigned_to"]}
            employees = [e for e in employees if e.pk in selected_ids]

        plannings = []
        if company and employees:
            qs = AssetPlanning.objects.filter(
                company=company,
                scheduled_date__range=(week_dates[0], week_dates[-1]),
                assigned_to_id__in=[e.pk for e in employees],
                scheduled_time__isnull=False,
            ).select_related(
                "asset", "origin", "destination", "transport_asset", "source_request", "superseded_by",
            )
            # Person rows already limited by assigned_to; apply the rest here.
            qs = _apply_planning_filters(qs, {**filters, "assigned_to": []})
            plannings = list(qs.order_by("scheduled_date", "scheduled_time"))

        if filters["only_with_plans"]:
            with_plans = {p.assigned_to_id for p in plannings}
            employees = [e for e in employees if e.pk in with_plans]

        person_index = {employee.pk: i for i, employee in enumerate(employees)}
        blocks = []
        for planning in plannings:
            if planning.assigned_to_id not in person_index:
                continue
            day_idx = (planning.scheduled_date - week_start).days
            end_time = planning.scheduled_end_time or _minutes_to_time(
                _time_to_minutes(planning.scheduled_time) + DEFAULT_BLOCK_MINUTES
            )
            blocks.append({
                **_planning_card_data(planning),
                "id": planning.pk,
                "person": person_index[planning.assigned_to_id],
                "day": day_idx,
                "start": _time_to_minutes(planning.scheduled_time),
                "end": _time_to_minutes(end_time),
                "origin": planning.assignment_origin,
            })

        # Backlog is a scheduling tool (drag-to-assign) — no point computing or
        # showing it to someone who can't act on it anyway.
        backlog_items = self._backlog_items(company, filters) if (company and can_edit) else []
        filter_items = _filter_query_items(request.GET)
        filter_querystring = urlencode(filter_items, doseq=True)

        context.update({
            "people": [{"pk": e.pk, "name": str(e)} for e in employees],
            "blocks": blocks,
            "backlog_items": backlog_items,
            "backlog_count": len(backlog_items),
            "week_start": week_start.isoformat(),
            "prev_week": (week_start - timedelta(days=7)).isoformat(),
            "next_week": (week_start + timedelta(days=7)).isoformat(),
            "has_company": company is not None,
            "can_edit": can_edit,
            "add_url": reverse("admin:assets_assetplanning_add"),
            "change_url_template": reverse("admin:assets_assetplanning_change", args=[0]),
            "capture_url": reverse("admin:assets_asset_schedule_capture"),
            "filter_form": filter_form,
            "filter_querystring": filter_querystring,
            "filters_active": bool(filter_items),
            "service_type_legend": [
                {"key": key, "label": label, "color": SERVICE_TYPE_COLORS[key]}
                for key, label in ServiceType.choices
            ],
        })
        return context

    @staticmethod
    def _backlog_items(company, filters=None):
        """Every planning still missing a driver, truck, and/or time — not renderable as a grid block yet."""
        filters = filters or {}
        qs = (
            AssetPlanning.objects.filter(company=company)
            .filter(
                Q(assigned_to__isnull=True)
                | Q(transport_asset__isnull=True)
                | Q(scheduled_time__isnull=True)
            )
            .select_related(
                "asset", "origin", "destination", "assigned_to", "transport_asset",
                "source_request", "superseded_by",
            )
            .order_by("scheduled_date")
        )
        backlog_filters = {**filters}
        if not backlog_filters.get("status"):
            backlog_filters = {**backlog_filters, "status": ["pending"]}
        qs = _apply_planning_filters(qs, backlog_filters)

        pending = list(qs)
        pending.sort(key=lambda p: URGENCY_ORDER.get(p.urgency, 2))

        optimizer = GoogleRouteOptimizer()
        items = []
        for p in pending:
            missing = []
            if not p.assigned_to_id:
                missing.append(_("driver"))
            if not p.transport_asset_id:
                missing.append(_("truck"))
            if not p.scheduled_time:
                missing.append(_("time"))
            duration = optimizer.estimate_duration(p.origin, p.destination)
            items.append({
                **_planning_card_data(p),
                "id": p.pk,
                "date": p.scheduled_date.isoformat(),
                "time_label": p.scheduled_time.strftime("%H:%M") if p.scheduled_time else "",
                "missing": missing,
                "driver": str(p.assigned_to) if p.assigned_to_id else "",
                "minutes": max(int(duration.total_seconds() // 60), 15),
            })
        return items

    # ── AJAX: move / resize / unassign a block ──────────────────────────
    def post(self, request, *args, **kwargs):
        if not request.user.has_perm("assets.change_assetplanning"):
            return JsonResponse({"error": _("Permission denied")}, status=403)

        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, TypeError):
            return JsonResponse({"error": _("Invalid payload")}, status=400)

        action = payload.get("action")
        allowed_companies = set(get_user_companies(request.user).values_list("id", flat=True))
        planning = get_object_or_404(AssetPlanning, pk=payload.get("id"))
        if planning.company_id not in allowed_companies:
            return JsonResponse({"error": _("Not found")}, status=404)

        if action == "unassign":
            planning.assigned_to = None
            planning.transport_asset = None
            planning.scheduled_time = None
            planning.estimated_duration = None
            planning.save(update_fields=["assigned_to", "transport_asset", "scheduled_time", "estimated_duration"])
            return JsonResponse({"success": True})

        if action in ("move", "resize"):
            try:
                start = _minutes_to_time(payload["start"])
                end = _minutes_to_time(payload["end"])
            except (KeyError, TypeError, ValueError):
                return JsonResponse({"error": _("Invalid time")}, status=400)
            if end <= start:
                return JsonResponse({"error": _("End time must be after start time")}, status=400)

            update_fields = ["scheduled_time", "estimated_duration"]
            planning.scheduled_time = start
            planning.estimated_duration = timedelta(minutes=_time_to_minutes(end) - _time_to_minutes(start))

            employee = None
            previous_driver_id = planning.assigned_to_id
            if action == "move":
                employee = get_object_or_404(Employee, pk=payload.get("person_id"), company_id=planning.company_id)
                try:
                    day = date.fromisoformat(payload.get("day", ""))
                except ValueError:
                    return JsonResponse({"error": _("Invalid date")}, status=400)
                planning.assigned_to = employee
                planning.scheduled_date = day
                update_fields += ["assigned_to", "scheduled_date"]

            planning.save(update_fields=update_fields)
            truck_note = auto_link_transport(planning, employee, previous_driver_id) if employee else None
            return JsonResponse({
                "success": True,
                "origin": planning.assignment_origin,
                "truck": planning.transport_asset.name if planning.transport_asset_id else "",
                "truck_note": truck_note,
            })

        if action == "duplicate_next_day":
            clone = AssetPlanning.objects.get(pk=planning.pk)
            clone.pk = None
            clone._state.adding = True
            clone.scheduled_date = planning.scheduled_date + timedelta(days=1)
            clone.save()
            clone.suggested_trucks.set(planning.suggested_trucks.all())
            clone.moved_employees.set(planning.moved_employees.all())

            end_time = clone.scheduled_end_time or _minutes_to_time(
                _time_to_minutes(clone.scheduled_time) + DEFAULT_BLOCK_MINUTES
            )
            return JsonResponse({
                "success": True,
                "block": {
                    **_planning_card_data(clone),
                    "id": clone.pk,
                    "assigned_to_id": clone.assigned_to_id,
                    "date": clone.scheduled_date.isoformat(),
                    "start": _time_to_minutes(clone.scheduled_time),
                    "end": _time_to_minutes(end_time),
                    "origin": clone.assignment_origin,
                },
            })

        if action == "auto_assign":
            from assets.services.transport_assigner import TransportAssigner

            if not TransportAssigner().assign_one(planning):
                return JsonResponse(
                    {"error": _("Could not auto-assign — no compatible truck/driver/time available.")},
                    status=400,
                )
            planning.refresh_from_db()
            end_time = planning.scheduled_end_time or _minutes_to_time(
                _time_to_minutes(planning.scheduled_time) + DEFAULT_BLOCK_MINUTES
            )
            return JsonResponse({
                "success": True,
                "block": {
                    **_planning_card_data(planning),
                    "id": planning.pk,
                    "assigned_to_id": planning.assigned_to_id,
                    "date": planning.scheduled_date.isoformat(),
                    "start": _time_to_minutes(planning.scheduled_time),
                    "end": _time_to_minutes(end_time),
                    "origin": planning.assignment_origin,
                },
            })

        return JsonResponse({"error": _("Invalid action")}, status=400)


def _fmt_minutes(minutes: int) -> str:
    minutes = ((minutes % 1440) + 1440) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class AssetScheduleCaptureView(_CompanyWeekMixin, UnfoldModelAdminViewMixin, TemplateView):
    """
    Screenshot-oriented schedule: full planning detail grouped by driver and day.
    Kept separate from the interactive grid so duration stays truthful there while
    transporters get every address / load / note in a capture-friendly layout.
    """
    template_name = "admin/assets/asset/schedule_capture.html"
    permission_required = ("assets.view_asset",)
    title = _("Schedule capture")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        company = self._company(request)
        week_start = self._week_start_param(request)
        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        today = timezone.localdate()

        day_raw = request.GET.get("day")
        selected_day = None
        if day_raw:
            try:
                selected_day = date.fromisoformat(day_raw)
            except ValueError:
                selected_day = None
        if selected_day is None:
            selected_day = today if week_start <= today <= week_dates[-1] else week_start
        if selected_day < week_start or selected_day > week_dates[-1]:
            selected_day = week_start

        person_raw = request.GET.get("person")
        selected_person_id = None
        if person_raw:
            try:
                selected_person_id = int(person_raw)
            except (TypeError, ValueError):
                selected_person_id = None

        employees = []
        if company:
            employees = list(
                Employee.objects.filter(company=company, is_active=True).order_by(
                    "first_name", "last_name"
                )
            )
        if selected_person_id and not any(e.pk == selected_person_id for e in employees):
            selected_person_id = None

        employee_ids = [selected_person_id] if selected_person_id else [e.pk for e in employees]
        plannings = []
        if company and employee_ids:
            plannings = list(
                AssetPlanning.objects.filter(
                    company=company,
                    scheduled_date=selected_day,
                    assigned_to_id__in=employee_ids,
                    scheduled_time__isnull=False,
                )
                .exclude(status="cancelled", superseded_by__isnull=True)
                .select_related("asset", "origin", "destination", "transport_asset", "assigned_to", "superseded_by")
                .order_by("assigned_to__first_name", "assigned_to__last_name", "scheduled_time")
            )

        jobs_by_person = {}
        for planning in plannings:
            end_time = planning.scheduled_end_time or _minutes_to_time(
                _time_to_minutes(planning.scheduled_time) + DEFAULT_BLOCK_MINUTES
            )
            start_m = _time_to_minutes(planning.scheduled_time)
            end_m = _time_to_minutes(end_time)
            card = {
                **_planning_card_data(planning),
                "id": planning.pk,
                "time_label": f"{_fmt_minutes(start_m)}–{_fmt_minutes(end_m)}",
                "start": start_m,
                "end": end_m,
                "driver": str(planning.assigned_to) if planning.assigned_to_id else "",
            }
            jobs_by_person.setdefault(planning.assigned_to_id, []).append(card)

        groups = []
        for employee in employees:
            if selected_person_id and employee.pk != selected_person_id:
                continue
            jobs = jobs_by_person.get(employee.pk, [])
            if not jobs and selected_person_id is None:
                continue
            groups.append({
                "person_id": employee.pk,
                "person_name": str(employee),
                "jobs": jobs,
                "job_count": len(jobs),
            })

        day_options = []
        for d in week_dates:
            day_options.append({
                "iso": d.isoformat(),
                "label": d.strftime("%a %d/%m"),
                "is_today": d == today,
                "selected": d == selected_day,
            })

        context.update({
            "has_company": company is not None,
            "company_name": company.name if company else "",
            "week_start": week_start.isoformat(),
            "prev_week": (week_start - timedelta(days=7)).isoformat(),
            "next_week": (week_start + timedelta(days=7)).isoformat(),
            "selected_day": selected_day.isoformat(),
            "selected_day_label": selected_day.strftime("%A %d/%m/%Y"),
            "day_options": day_options,
            "people": [{"pk": e.pk, "name": str(e)} for e in employees],
            "selected_person_id": selected_person_id,
            "groups": groups,
            "total_jobs": sum(g["job_count"] for g in groups),
            "schedule_url": reverse("admin:assets_asset_schedule") + f"?week={week_start.isoformat()}",
            "capture_url": reverse("admin:assets_asset_schedule_capture"),
        })
        return context
