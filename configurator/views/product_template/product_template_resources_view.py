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

from configurator.models import ProductTemplate
from configurator.forms.stage_resources_form import (
    EmployeeFormSet,
    MachineFormSet,
    ResourcesFormsetHelper,
    StagePlanningFormSet,
)


class ProductTemplateResourcesView(UnfoldModelAdminViewMixin, View):
    title = _("Configure planning resources")
    permission_required = "configuration.configure_stage_resources"
    template_name = "admin/configurator/producttemplate/planning.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.obj = get_object_or_404(ProductTemplate, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def _build_outer_initial(self):
        stage_keys = list(self.obj.production_stages or [])
        existing = self.obj.production_planning_resources or {}

        initial = []
        for key in stage_keys:
            stage = existing.get(key, {})
            initial.append(
                {
                    "stage_key": key,
                    "prep_time_min": stage.get("prep_time_min", 0),
                    "work_time_min": stage.get("work_time_min", 0),
                    "post_time_min": stage.get("post_time_min", 0),
                }
            )
        return initial

    def _build_inner_initial_for_stage(self, key: str):
        existing = self.obj.production_planning_resources or {}
        stage = existing.get(key, {})

        emp_rows = stage.get("employee_types", []) or []
        mac_rows = stage.get("machine_types", []) or []

        emp_initial = [
            {
                "employee_type": r.get("employee_type_id"),
                "employee_qty": r.get("quantity", 0),
            }
            for r in emp_rows
        ]
        mac_initial = [
            {
                "machine_type": r.get("machine_type_id"),
                "machine_qty": r.get("quantity", 0),
            }
            for r in mac_rows
        ]
        return emp_initial, mac_initial

    def _build_inner_formsets(self, stage_forms, data=None):
        inner = []
        for i, stage_form in enumerate(stage_forms):
            key = stage_form.initial.get("stage_key") or stage_form.data.get(
                f"{stage_form.prefix}-stage_key"
            )

            emp_prefix = f"stages-{i}-employees"
            mac_prefix = f"stages-{i}-machines"

            if data is None:
                emp_initial, mac_initial = self._build_inner_initial_for_stage(key)
                emp_fs = EmployeeFormSet(
                    prefix=emp_prefix, company=self.obj.company, initial=emp_initial
                )
                mac_fs = MachineFormSet(
                    prefix=mac_prefix, company=self.obj.company, initial=mac_initial
                )
            else:
                emp_fs = EmployeeFormSet(
                    data=data, prefix=emp_prefix, company=self.obj.company
                )
                mac_fs = MachineFormSet(
                    data=data, prefix=mac_prefix, company=self.obj.company
                )

            emp_helper = ResourcesFormsetHelper(form_id=f"{emp_prefix}-table")
            mac_helper = ResourcesFormsetHelper(form_id=f"{mac_prefix}-table")

            inner.append(
                {
                    "employees": (emp_fs, emp_helper),
                    "machines": (mac_fs, mac_helper),
                }
            )
        return inner

    def _render(self, request, stage_formset, inner):
        context = {
            **self.model_admin.admin_site.each_context(request),
            "title": _("Configure planning resources: %(name)s")
            % {"name": self.obj.name},
            "obj": self.obj,
            "stage_formset": stage_formset,
            "stage_blocks": list(zip(stage_formset.forms, inner)),
        }
        return TemplateResponse(request, self.template_name, context)

    def get(self, request, *args, **kwargs):
        stage_formset = StagePlanningFormSet(
            initial=self._build_outer_initial(),
            prefix="stages",
            company=self.obj.company,
            request=request,
            admin_site=self.model_admin.admin_site,
        )
        inner = self._build_inner_formsets(stage_formset.forms, data=None)
        return self._render(request, stage_formset, inner)

    def post(self, request, *args, **kwargs):
        stage_formset = StagePlanningFormSet(
            request.POST,
            prefix="stages",
            company=self.obj.company,
            request=request,
            admin_site=self.model_admin.admin_site,
        )
        inner = self._build_inner_formsets(stage_formset.forms, data=request.POST)

        all_valid = stage_formset.is_valid()
        for block in inner:
            emp_fs, __ = block["employees"]
            mac_fs, __ = block["machines"]
            all_valid = emp_fs.is_valid() and all_valid
            all_valid = mac_fs.is_valid() and all_valid

        if not all_valid:
            messages.error(request, _("Please correct the errors below."))
            return self._render(request, stage_formset, inner)

        new_data = {}
        try:
            for i, stage_form in enumerate(stage_formset.forms):
                key = stage_form.cleaned_data["stage_key"]

                prep = stage_form.cleaned_data["prep_time_min"]
                work = stage_form.cleaned_data["work_time_min"]
                post = stage_form.cleaned_data["post_time_min"]
                total = prep + work + post

                emp_fs, __ = inner[i]["employees"]
                mac_fs, __ = inner[i]["machines"]

                employees = []
                for row in emp_fs.cleaned_data:
                    if not row or row.get("DELETE"):
                        continue
                    et = row.get("employee_type")
                    eq = row.get("employee_qty") or 0
                    if et and eq > 0:
                        employees.append({"employee_type_id": et.id, "quantity": eq})

                machines = []
                for row in mac_fs.cleaned_data:
                    if not row or row.get("DELETE"):
                        continue
                    mt = row.get("machine_type")
                    mq = row.get("machine_qty") or 0
                    if mt and mq > 0:
                        machines.append({"machine_type_id": mt.id, "quantity": mq})

                new_data[key] = {
                    "employee_types": employees,
                    "machine_types": machines,
                    "prep_time_min": prep,
                    "work_time_min": work,
                    "post_time_min": post,
                    "total_time_min": total,
                }

            self.obj.production_planning_resources = new_data
            self.obj.production_planning_configured = True
            self.obj.full_clean()
            self.obj.save()

        except ValidationError as e:
            messages.error(request, _("Could not save: %(msg)s") % {"msg": e})
            return self._render(request, stage_formset, inner)

        messages.success(request, _("Planning resources saved."))
        return redirect("admin:configurator_producttemplate_changelist")
