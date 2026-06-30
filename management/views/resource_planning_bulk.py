from __future__ import annotations

import json

from django.contrib.admin.widgets import AutocompleteSelect
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone as tz
from django.utils.translation import gettext_lazy as _
from django.views import View
from unfold.forms import AdminForm as UnfoldAdminForm
from unfold.views import UnfoldModelAdminViewMixin
from unfold.widgets import UnfoldAdminDateWidget, UnfoldAdminTimeWidget, UnfoldBooleanSwitchWidget

from core.admin.company_access import get_user_companies
from management.forms import ResourcePlanningBulkForm
from management.models import ResourcePlanning, ResourcePlanningGroup, ResourcePlanningStatus
from management.services.resource_planning import (
    create_plannings,
    delete_day_from_planning,
    dissolve_group,
    update_group,
    update_planning,
)


class _PlanningFormMixin:
    """Shared helpers for the add and group-change views."""

    FIELDSETS = [
        (_("Planning"), {"fields": ("company", "project_file", "planning_type", "status")}),
        (_("Schedule"), {"fields": (("date_from", "date_to"), ("daily_start", "daily_end"), "exclude_weekends")}),
        (_("Options"), {"fields": ("is_group", "notes")}),
    ]

    RESOURCE_MODEL_MAP = {
        "employee": ("company", "Employee", lambda e: f"{e.last_name}, {e.first_name} ({_('Employee')})"),
        "machine":  ("company", "Machine",  lambda m: f"{m.name} ({_('Machine')})"),
        "asset":    ("assets",  "Asset",    lambda a: f"{a.name} ({_('Asset')})"),
    }

    def _get_form(self, request, data=None, initial=None) -> ResourcePlanningBulkForm:
        return ResourcePlanningBulkForm(
            data=data,
            initial=initial,
            user=request.user,
            user_companies=get_user_companies(request.user),
        )

    def _apply_form_widgets(self, form: ResourcePlanningBulkForm, request) -> None:
        """Wire autocomplete widgets for FK fields after form instantiation."""
        for field_name in ("company", "project_file", "planning_type"):
            if field_name not in form.fields:
                continue
            db_field = ResourcePlanning._meta.get_field(field_name)
            widget = AutocompleteSelect(db_field, self.model_admin.admin_site)
            form.fields[field_name].widget = widget
            widget.choices = form.fields[field_name].choices

    def _build_adminform(self, form: ResourcePlanningBulkForm) -> UnfoldAdminForm:
        return UnfoldAdminForm(
            form=form,
            fieldsets=self.FIELDSETS,
            prepopulated_fields={},
            readonly_fields=[],
            model_admin=self.model_admin,
        )

    def _get_context(self, request, form, group, errors, delete_url=None) -> dict:
        page_title = _("Edit Resource Planning") if group else _("Create Resource Planning")
        return {
            **self.model_admin.admin_site.each_context(request),
            "model_admin": self.model_admin,
            "content_title": str(page_title),
            "title": page_title,
            "adminform": self._build_adminform(form),
            "form": form,
            "media": self.model_admin.media + form.media,
            "group": group,
            "cancel_url": reverse("admin:management_resourceplanning_changelist"),
            "delete_url": delete_url,
            "errors": errors,
            "has_file_field": False,
        }

    @classmethod
    def _resolve_resource_labels(cls, raw: str) -> dict:
        from django.apps import apps

        labels = {}
        for token in raw.split(","):
            token = token.strip()
            if not token or ":" not in token:
                continue
            kind, obj_id_str = token.split(":", 1)
            if kind not in cls.RESOURCE_MODEL_MAP:
                continue
            try:
                obj_id = int(obj_id_str)
            except ValueError:
                continue
            app_label, model_name, fmt = cls.RESOURCE_MODEL_MAP[kind]
            try:
                Model = apps.get_model(app_label, model_name)
                labels[token] = fmt(Model.objects.get(pk=obj_id))
            except Exception:
                pass
        return labels

    def _resource_labels_json(self, raw: str) -> str:
        return json.dumps(self._resolve_resource_labels(raw) if raw else {})


class ResourcePlanningAddView(_PlanningFormMixin, UnfoldModelAdminViewMixin, View):
    title = _("Create Resource Planning")
    permission_required = ("management.add_resourceplanning",)

    def get(self, request, *args, **kwargs):
        initial = {}
        if request.GET.get("resources"):
            initial["resources"] = request.GET["resources"]
        if request.GET.get("company"):
            initial["company"] = request.GET["company"]
        if request.GET.get("day"):
            from django.utils.dateparse import parse_date
            day = parse_date(request.GET["day"])
            if day:
                initial.update({"date_from": day, "date_to": day})

        form = self._get_form(request, initial=initial)
        self._apply_form_widgets(form, request)
        ctx = self._get_context(request, form, None, [])
        ctx["resource_labels_json"] = self._resource_labels_json(initial.get("resources", ""))
        return render(request, "admin/management/resourceplanning/planning_form.html", ctx)

    def post(self, request, *args, **kwargs):
        form = self._get_form(request, data=request.POST)
        errors = []
        if form.is_valid():
            try:
                cd = form.cleaned_data
                plannings = create_plannings(
                    company=cd["company"],
                    project_file=cd["project_file"],
                    planning_type=cd["planning_type"],
                    status=cd["status"],
                    notes=cd.get("notes", ""),
                    resources=form.resource_pairs,
                    date_from=cd["date_from"],
                    date_to=cd["date_to"],
                    daily_start=cd["daily_start"],
                    daily_end=cd["daily_end"],
                    exclude_weekends=cd["exclude_weekends"],
                    is_group=cd.get("is_group", False),
                )
                self.model_admin.message_user(
                    request,
                    _(f"Created {len(plannings)} resource planning(s) successfully."),
                )
                add_url = reverse("admin:management_resourceplanning_bulk_add")
                if "_addanother" in request.POST:
                    return HttpResponseRedirect(add_url)
                group = plannings[0].group if plannings and plannings[0].group_id else None
                if group:
                    return HttpResponseRedirect(
                        reverse("admin:management_resourceplanning_group_change", args=[group.pk])
                    )
                if plannings and "_continue" in request.POST:
                    return HttpResponseRedirect(
                        reverse("admin:management_resourceplanning_individual_change", args=[plannings[0].pk])
                    )
                return HttpResponseRedirect(reverse("admin:management_resourceplanning_changelist"))
            except ValidationError as e:
                errors = e.messages

        raw = request.POST.get("resources", "")
        self._apply_form_widgets(form, request)
        ctx = self._get_context(request, form, None, errors)
        ctx["resource_labels_json"] = self._resource_labels_json(raw)
        return render(request, "admin/management/resourceplanning/planning_form.html", ctx)


class ResourcePlanningGroupChangeView(_PlanningFormMixin, UnfoldModelAdminViewMixin, View):
    title = _("Edit Resource Planning Group")
    permission_required = ("management.change_resourceplanning",)

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.group = get_object_or_404(ResourcePlanningGroup, pk=kwargs["group_pk"])

    def _resource_tokens(self) -> str:
        """Derive comma-separated resource tokens from the group's plannings."""
        seen, tokens = set(), []
        for p in self.group.plannings.select_related("resource_content_type").order_by("start"):
            key = (p.resource_content_type_id, p.resource_object_id)
            if key not in seen:
                seen.add(key)
                tokens.append(f"{p.resource_content_type.model}:{p.resource_object_id}")
        return ",".join(tokens)

    def _initial_from_group(self) -> dict:
        plannings = list(
            self.group.plannings.select_related(
                "project_file", "planning_type", "company"
            ).order_by("start")
        )
        if not plannings:
            return {}
        first, last = plannings[0], plannings[-1]
        local_first_start = tz.localtime(first.start)
        local_first_end   = tz.localtime(first.end)
        local_last_end    = tz.localtime(last.end)
        return {
            "company": first.company_id,
            "project_file": first.project_file_id,
            "planning_type": first.planning_type_id,
            "status": first.status,
            "notes": first.notes,
            "resources": self._resource_tokens(),
            "date_from": local_first_start.date(),
            "date_to": local_last_end.date(),
            "daily_start": local_first_start.time(),
            "daily_end": local_last_end.time(),
            "exclude_weekends": first.exclude_weekends,
            "is_group": True,
        }

    def get(self, request, *args, **kwargs):
        initial = self._initial_from_group()
        form = self._get_form(request, initial=initial)
        self._apply_form_widgets(form, request)
        ctx = self._get_context(request, form, self.group, [])
        ctx["resource_labels_json"] = self._resource_labels_json(initial.get("resources", ""))
        ctx["plannings"] = list(
            self.group.plannings.select_related("resource_content_type").order_by("start")
        )
        return render(request, "admin/management/resourceplanning/planning_form.html", ctx)

    def post(self, request, *args, **kwargs):
        group = self.group
        add_url = reverse("admin:management_resourceplanning_bulk_add")

        if "cancel_group" in request.POST:
            group.cancel_all()
            self.model_admin.message_user(request, _("Group cancelled."))
            return HttpResponseRedirect(reverse("admin:management_resourceplanning_changelist"))

        form = self._get_form(request, data=request.POST)
        errors = []
        if form.is_valid():
            try:
                cd = form.cleaned_data
                is_group = cd.get("is_group", True)

                update_group(
                    group.pk,
                    company=cd["company"],
                    project_file=cd["project_file"],
                    planning_type=cd["planning_type"],
                    status=cd["status"],
                    notes=cd.get("notes", ""),
                    resources=form.resource_pairs,
                    date_from=cd["date_from"],
                    date_to=cd["date_to"],
                    daily_start=cd["daily_start"],
                    daily_end=cd["daily_end"],
                    exclude_weekends=cd["exclude_weekends"],
                )
                if not is_group:
                    dissolve_group(group.pk)
                    self.model_admin.message_user(
                        request, _("Group dissolved. Plannings are now individual.")
                    )
                    return HttpResponseRedirect(reverse("admin:management_resourceplanning_changelist"))

                if "_addanother" in request.POST:
                    self.model_admin.message_user(request, _("Group updated successfully."))
                    return HttpResponseRedirect(add_url)
                group_change_url = reverse(
                    "admin:management_resourceplanning_group_change", args=[group.pk]
                )
                self.model_admin.message_user(request, _("Group updated successfully."))
                return HttpResponseRedirect(group_change_url)
            except ValidationError as e:
                errors = e.messages

        raw = request.POST.get("resources", "")
        self._apply_form_widgets(form, request)
        ctx = self._get_context(request, form, group, errors)
        ctx["resource_labels_json"] = self._resource_labels_json(raw)
        ctx["plannings"] = list(
            group.plannings.select_related("resource_content_type").order_by("start")
        )
        return render(request, "admin/management/resourceplanning/planning_form.html", ctx)


class ResourcePlanningIndividualChangeView(_PlanningFormMixin, UnfoldModelAdminViewMixin, View):
    title = _("Edit Resource Planning")
    permission_required = ("management.change_resourceplanning",)

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        from management.models import ResourcePlanning
        self.planning = get_object_or_404(ResourcePlanning, pk=kwargs["pk"])

    def _resource_token(self) -> str:
        p = self.planning
        return f"{p.resource_content_type.model}:{p.resource_object_id}"

    def _initial_from_planning(self) -> dict:
        p = self.planning
        local_start = tz.localtime(p.start)
        local_end   = tz.localtime(p.end)
        return {
            "company": p.company_id,
            "project_file": p.project_file_id,
            "planning_type": p.planning_type_id,
            "status": p.status,
            "notes": p.notes,
            "resources": self._resource_token(),
            "date_from": local_start.date(),
            "date_to": local_end.date(),
            "daily_start": local_start.time(),
            "daily_end": local_end.time(),
            "exclude_weekends": p.exclude_weekends,
            "is_group": False,
        }

    def get(self, request, *args, **kwargs):
        initial = self._initial_from_planning()
        form = self._get_form(request, initial=initial)
        self._apply_form_widgets(form, request)
        delete_url = reverse("admin:management_resourceplanning_delete", args=[self.planning.pk])
        ctx = self._get_context(request, form, None, [], delete_url=delete_url)
        ctx["resource_labels_json"] = self._resource_labels_json(self._resource_token())
        ctx["planning"] = self.planning
        return render(request, "admin/management/resourceplanning/planning_form.html", ctx)

    def post(self, request, *args, **kwargs):
        planning = self.planning
        add_url = reverse("admin:management_resourceplanning_bulk_add")
        individual_change_url = reverse(
            "admin:management_resourceplanning_individual_change", args=[planning.pk]
        )
        form = self._get_form(request, data=request.POST)
        errors = []
        if form.is_valid():
            try:
                cd = form.cleaned_data
                resource_pairs = form.resource_pairs
                is_group = cd.get("is_group", False)

                import datetime
                from django.utils import timezone as _tz
                start_dt = _tz.make_aware(
                    datetime.datetime.combine(cd["date_from"], cd["daily_start"])
                )
                end_dt = _tz.make_aware(
                    datetime.datetime.combine(cd["date_to"], cd["daily_end"])
                )

                if len(resource_pairs) > 1:
                    # Multiple resources: delete old planning and bulk-create new ones
                    # (grouped or individual depending on is_group).
                    from django.db import transaction as _tx
                    with _tx.atomic():
                        planning.delete()
                        new_plannings = create_plannings(
                            company=cd["company"],
                            project_file=cd["project_file"],
                            planning_type=cd["planning_type"],
                            status=cd["status"],
                            notes=cd.get("notes", ""),
                            resources=resource_pairs,
                            date_from=cd["date_from"],
                            date_to=cd["date_to"],
                            daily_start=cd["daily_start"],
                            daily_end=cd["daily_end"],
                            exclude_weekends=cd["exclude_weekends"],
                            is_group=is_group,
                        )
                    if "_addanother" in request.POST:
                        return HttpResponseRedirect(add_url)
                    if is_group:
                        self.model_admin.message_user(
                            request, _("Planning converted to group successfully.")
                        )
                        return HttpResponseRedirect(
                            reverse(
                                "admin:management_resourceplanning_group_change",
                                args=[new_plannings[0].group.pk],
                            )
                        )
                    self.model_admin.message_user(
                        request, _("Created %(n)s individual plannings.") % {"n": len(new_plannings)}
                    )
                    return HttpResponseRedirect(reverse("admin:management_resourceplanning_changelist"))

                # Single resource — plain update
                ct, obj_id = resource_pairs[0]
                update_planning(
                    planning.pk,
                    company=cd["company"],
                    project_file=cd["project_file"],
                    planning_type=cd["planning_type"],
                    status=cd["status"],
                    notes=cd.get("notes", ""),
                    exclude_weekends=cd["exclude_weekends"],
                    resource_content_type=ct,
                    resource_object_id=obj_id,
                    start=start_dt,
                    end=end_dt,
                )
                self.model_admin.message_user(request, _("Planning updated successfully."))
                if "_addanother" in request.POST:
                    return HttpResponseRedirect(add_url)
                if "_continue" in request.POST:
                    return HttpResponseRedirect(individual_change_url)
                return HttpResponseRedirect(reverse("admin:management_resourceplanning_changelist"))
            except ValidationError as e:
                errors = e.messages

        raw = request.POST.get("resources", "")
        self._apply_form_widgets(form, request)
        delete_url = reverse("admin:management_resourceplanning_delete", args=[planning.pk])
        ctx = self._get_context(request, form, None, errors, delete_url=delete_url)
        ctx["resource_labels_json"] = self._resource_labels_json(raw)
        ctx["planning"] = planning
        return render(request, "admin/management/resourceplanning/planning_form.html", ctx)


class ResourcePlanningDeleteDayView(UnfoldModelAdminViewMixin, View):
    """Remove a single calendar day from a planning's date range."""

    title = _("Delete Day")
    permission_required = ("management.change_resourceplanning",)

    def post(self, request, pk, *args, **kwargs):
        from django.utils.dateparse import parse_date

        day_str = request.POST.get("day", "")
        day = parse_date(day_str)
        if day:
            delete_day_from_planning(pk, day)

        next_url = request.POST.get("next") or reverse(
            "admin:management_resourceplanning_schedule"
        )
        return HttpResponseRedirect(next_url)
