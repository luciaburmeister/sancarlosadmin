from django.core.exceptions import ValidationError
from django.template.response import TemplateResponse
from django.http import HttpRequest
from django.http.response import HttpResponse
from typing import Any
from django.views import View
from unfold.views import UnfoldModelAdminViewMixin
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _

from configurator.forms.stage_resources_form import StagePlanningFormSet
from configurator.models import ProductTemplate
from production.services.actual_stage_times import (
    compute_actual_stage_times,
    normalize_stage,
)


class ProductTemplateResourcesView(UnfoldModelAdminViewMixin, View):
    title = _("Configure planning resources")
    permission_required = "configuration.configure_stage_resources"
    template_name = "admin/configurator/producttemplate/planning.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.obj = get_object_or_404(ProductTemplate, pk=kwargs["pk"])
        self.actuals = compute_actual_stage_times(self.obj)
        return super().dispatch(request, *args, **kwargs)

    def _actual_for_stage(self, stage_key):
        return self.actuals.get(normalize_stage(stage_key))

    def _build_outer_initial(self, prefill_actuals=False):
        stage_keys = list(self.obj.production_stages or [])
        existing = self.obj.production_planning_resources or {}

        initial = []
        for key in stage_keys:
            stage = existing.get(key, {})
            row = {
                "stage_key": key,
                "employee_qty": stage.get("employee_qty", 0),
                "machine_qty": stage.get("machine_qty", 0),
                "prep_time_min": stage.get("prep_time_min", 0),
                "work_time_min": stage.get("work_time_min", 0),
                "post_time_min": stage.get("post_time_min", 0),
            }

            if prefill_actuals:
                actual = self._actual_for_stage(key)
                if actual:
                    row["work_time_min"] = str(int(round(actual.avg_minutes)))
                    if actual.avg_employees:
                        row["employee_qty"] = max(1, int(round(actual.avg_employees)))

            initial.append(row)
        return initial

    def _render(self, request, stage_formset):
        for stage_form in stage_formset.forms:
            key = stage_form.initial.get("stage_key") or stage_form.data.get(
                f"{stage_form.prefix}-stage_key"
            )
            stage_form.actual = self._actual_for_stage(key)

        context = {
            **self.model_admin.admin_site.each_context(request),
            "title": _("Configure planning resources: %(name)s") % {"name": self.obj.name},
            "obj": self.obj,
            "stage_formset": stage_formset,
            "stage_blocks": stage_formset.forms,
            "has_actuals": any(
                self._actual_for_stage(key)
                for key in (self.obj.production_stages or [])
            ),
        }
        return TemplateResponse(request, self.template_name, context)

    def get(self, request, *args, **kwargs):
        prefill_actuals = request.GET.get("prefill") == "actuals"
        stage_formset = StagePlanningFormSet(
            initial=self._build_outer_initial(prefill_actuals=prefill_actuals),
            prefix="stages",
        )
        if prefill_actuals:
            messages.info(
                request,
                _(
                    "Times prefilled from actual production data. "
                    "Review the values and save to apply them."
                ),
            )
        return self._render(request, stage_formset)

    def post(self, request, *args, **kwargs):
        stage_formset = StagePlanningFormSet(
            request.POST,
            prefix="stages",
        )

        if not stage_formset.is_valid():
            messages.error(request, _("Please correct the errors below."))
            return self._render(request, stage_formset)

        new_data = {}
        try:
            for stage_form in stage_formset.forms:
                key = stage_form.cleaned_data["stage_key"]

                prep = stage_form.cleaned_data["prep_time_min"]
                work = stage_form.cleaned_data["work_time_min"]
                post = stage_form.cleaned_data["post_time_min"]
                emp_qty = stage_form.cleaned_data["employee_qty"]
                mach_qty = stage_form.cleaned_data["machine_qty"]

                new_data[key] = {
                    "employee_qty": emp_qty,
                    "machine_qty": mach_qty,
                    "prep_time_min": prep,
                    "work_time_min": work,
                    "post_time_min": post,
                }

            self.obj.production_planning_resources = new_data
            self.obj.production_planning_configured = True
            self.obj.full_clean()
            self.obj.save()

        except ValidationError as e:
            messages.error(request, _("Could not save: %(msg)s") % {"msg": e})
            return self._render(request, stage_formset)

        messages.success(request, _("Planning resources saved."))
        return redirect("admin:configurator_producttemplate_changelist")
