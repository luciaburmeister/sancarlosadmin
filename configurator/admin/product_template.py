from django.contrib import admin, messages
from unfold.admin import ModelAdmin
from configurator.forms.stage_resources_form import EmployeeFormSet, MachineFormSet, ResourcesFormsetHelper, StagePlanningFormSet
from configurator.models import ProductTemplate, ProductParameter, BOMRule, PricingRuleTemplate, ProductComputedVariable
from unfold.admin import TabularInline,StackedInline,GenericTabularInline
from django.db import models
from unfold.widgets import UnfoldAdminTextareaWidget
from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField
from django.utils.translation import gettext_lazy as _
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from invoices.models import AppliedTax
from unfold.decorators import action
from django.utils.html import format_html
from django.core.exceptions import ValidationError
from django.template.response import TemplateResponse

class AppliedTaxInline(GenericTabularInline):
    ordering_field = "order"
    hide_ordering_field=True
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    fields = ("tax_type", "selected_percentage", "order")
    verbose_name = _("Applied tax")
    verbose_name_plural = _("Applied taxes")
    tab = True



class ProductComputedVariableInline(TabularInline):
    model = ProductComputedVariable
    extra = 0
    fields = ("name", "expression")
    verbose_name = _("Computed Variable")
    verbose_name_plural = _("Computed Variables")
    tab = True


class ProductParameterInline(TabularInline):
    model = ProductParameter
    extra = 0
    tab=True
    hide_ordering_field = True
    ordering_field = "order"
    
    formfield_overrides = {
        models.JSONField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }

class BOMRuleInline(StackedInline):
    model = BOMRule
    extra = 0
    fields = ("component", ("quantity_expression", "length_expression"),("production_stages","description"))
    tab=True
    autocomplete_fields = ["component"]

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)

        if obj and obj.production_stages:
            formset.form.base_fields["production_stages"].widget = ArrayWidget(
                choices=[("", "---------")] + [(s, s) for s in obj.production_stages]
            )

        return formset

    

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("component","product")


class PricingRuleTemplateInline(TabularInline):
    model = PricingRuleTemplate
    extra = 0
    fields = ("name", "expression", "is_active")
    tab=True

    formfield_overrides = {
        models.TextField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
    }


@admin.register(ProductTemplate)
class ProductTemplateAdmin(ModelAdmin):
    list_display = ("id", "name","company", "planning_configured_badge", "planning_configure_link")
    search_fields = ("name",)
    actions_row = ["duplicate_product"]
    list_select_related = ["company"]
    autocomplete_fields = ["company"]

    inlines = [
        ProductParameterInline,
        ProductComputedVariableInline,
        BOMRuleInline,
        PricingRuleTemplateInline,
        AppliedTaxInline, 
    ]

    fieldsets = (
        (_("Product Template"), {
            "fields": (("name", "image"),"description","production_stages"),
        })
        ,(_("Ownership"), {
            "fields": ("company",),
        })
    )

    @admin.display(description=_("Planning configured"), boolean=True)
    def planning_configured_badge(self, obj: ProductTemplate):
        return bool(obj.production_planning_configured)

    @admin.display(description=_("Planning"))
    def planning_configure_link(self, obj: ProductTemplate):
        url = reverse("admin:configurator_producttemplate_planning", args=[obj.pk])
        return format_html('<a class="button" href="{}">{}</a>', url, _("Configure"))

    formfield_overrides = {
        models.TextField: {"widget": UnfoldAdminTextareaWidget(attrs={"rows": 2})},
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    @action(
        description=_("Clone"),
        url_path="producttemplate-admin-clone",
    )
    def duplicate_product(self, request, object_id: int):
        """
        Clone this product template and redirect to the new copy.
        """
        obj = ProductTemplate.objects.get(pk=object_id)
        new_product = obj.clone()

        self.message_user(
            request,
            _("Product template '%(name)s' was successfully duplicated.")
            % {"name": obj.name},
        )

        return redirect(
            reverse(
                "admin:configurator_producttemplate_change",
                args=[new_product.pk],
            )
        )
    
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:pk>/planning/",
                self.admin_site.admin_view(self.planning_view),
                name="configurator_producttemplate_planning",
            ),
        ]
        return custom + urls

    def planning_view(self, request, pk: int):
        obj = get_object_or_404(ProductTemplate, pk=pk)

        stage_keys = list(obj.production_stages or [])
        existing = obj.production_planning_resources or {}

        def build_outer_initial():
            initial = []
            for key in stage_keys:
                stage = existing.get(key, {})
                initial.append({
                    "stage_key": key,
                    "prep_time_min": stage.get("prep_time_min", 0),
                    "work_time_min": stage.get("work_time_min", 0),
                    "post_time_min": stage.get("post_time_min", 0),
                })
            return initial

        def build_inner_initial_for_stage(key: str):
            stage = existing.get(key, {})
            emp_rows = stage.get("employee_types", []) or []
            mac_rows = stage.get("machine_types", []) or []

            emp_initial = [
                {"employee_type": r.get("employee_type_id"), "employee_qty": r.get("quantity", 0)}
                for r in emp_rows
            ]
            mac_initial = [
                {"machine_type": r.get("machine_type_id"), "machine_qty": r.get("quantity", 0)}
                for r in mac_rows
            ]
            return emp_initial, mac_initial

        def build_inner_formsets(stage_forms, data=None):
            """
            Returns a list aligned with stage_forms:
            inner[i] = {"employees": (emp_fs, emp_helper), "machines": (mac_fs, mac_helper)}
            """
            inner = []
            for i, stage_form in enumerate(stage_forms):
                key = stage_form.initial.get("stage_key") or stage_form.data.get(f"{stage_form.prefix}-stage_key")

                emp_prefix = f"stages-{i}-employees"
                mac_prefix = f"stages-{i}-machines"

                if data is None:
                    emp_initial, mac_initial = build_inner_initial_for_stage(key)
                    emp_fs = EmployeeFormSet(prefix=emp_prefix, company=obj.company, initial=emp_initial)
                    mac_fs = MachineFormSet(prefix=mac_prefix, company=obj.company, initial=mac_initial)
                else:
                    emp_fs = EmployeeFormSet(data=data, prefix=emp_prefix, company=obj.company)
                    mac_fs = MachineFormSet(data=data, prefix=mac_prefix, company=obj.company)

                emp_helper = ResourcesFormsetHelper(form_id=f"{emp_prefix}-table")
                mac_helper = ResourcesFormsetHelper(form_id=f"{mac_prefix}-table")

                inner.append({
                    "employees": (emp_fs, emp_helper),
                    "machines": (mac_fs, mac_helper),
                })
            return inner

        if request.method == "POST":
            stage_formset = StagePlanningFormSet(
                request.POST,
                prefix="stages",
                company=obj.company,
                request=request,
                admin_site=self.admin_site,
            )

            inner = build_inner_formsets(stage_formset.forms, data=request.POST)

            all_valid = stage_formset.is_valid()
            for block in inner:
                emp_fs, __ = block["employees"]
                mac_fs, __ = block["machines"]
                all_valid = emp_fs.is_valid() and all_valid
                all_valid = mac_fs.is_valid() and all_valid

            if all_valid:
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
                            if not row:
                                continue
                            if row.get("DELETE"):
                                continue
                            et = row.get("employee_type")
                            eq = row.get("employee_qty") or 0
                            if et and eq > 0:
                                employees.append({"employee_type_id": et.id, "quantity": eq})

                        machines = []
                        for row in mac_fs.cleaned_data:
                            if not row:
                                continue
                            if row.get("DELETE"):
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

                    obj.production_planning_resources = new_data
                    obj.production_planning_configured = True
                    obj.full_clean()
                    obj.save()

                except ValidationError as e:
                    messages.error(request, _("Could not save: %(msg)s") % {"msg": e})
                else:
                    messages.success(request, _("Planning resources saved."))
                    return redirect("admin:configurator_producttemplate_changelist")
            else:
                messages.error(request, _("Please correct the errors below."))

        else:
            stage_formset = StagePlanningFormSet(
                initial=build_outer_initial(),
                prefix="stages",
                company=obj.company,
                request=request,
                admin_site=self.admin_site,
            )
            inner = build_inner_formsets(stage_formset.forms, data=None)

        stage_blocks = list(zip(stage_formset.forms, inner))

        context = {
            **self.admin_site.each_context(request),
            "title": _("Configure planning resources: %(name)s") % {"name": obj.name},
            "obj": obj,
            "stage_formset": stage_formset,
            "stage_blocks": stage_blocks,
        }
        return TemplateResponse(
            request,
            "admin/configurator/producttemplate/planning.html",
            context,
        )
