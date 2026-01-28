from django.contrib import admin, messages
from unfold.admin import ModelAdmin
from configurator.forms.stage_resources_form import StagePlanningFormSet
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

        initial = []
        for key in stage_keys:
            stage = existing.get(key, {})

            employee_types = stage.get("employee_types", [])
            emp_row = employee_types[0] if employee_types else {}
            
            machine_types = stage.get("machine_types", [])
            mac_row = machine_types[0] if machine_types else {}

            initial.append({
                "stage_key": key,
                "employee_type": emp_row.get("employee_type_id", None),
                "employee_qty": emp_row.get("quantity", 0),
                "machine_type": mac_row.get("machine_type_id", None),
                "machine_qty": mac_row.get("quantity", 0),
                "prep_time_min": stage.get("prep_time_min", 0),
                "work_time_min": stage.get("work_time_min", 0),
                "post_time_min": stage.get("post_time_min", 0),
            })

        if request.method == "POST":
            formset = StagePlanningFormSet(
                request.POST,
                company=obj.company,
                request=request,
                admin_site=self.admin_site,
            )

            if formset.is_valid():
                new_data = {}

                try:
                    for form in formset:
                        key = form.cleaned_data["stage_key"]

                        et = form.cleaned_data.get("employee_type")
                        eq = form.cleaned_data.get("employee_qty", 0)
                        mt = form.cleaned_data.get("machine_type")
                        mq = form.cleaned_data.get("machine_qty", 0)

                        prep = form.cleaned_data["prep_time_min"]
                        work = form.cleaned_data["work_time_min"]
                        post = form.cleaned_data["post_time_min"]
                        total = prep + work + post

                        new_data[key] = {
                            "employee_types": (
                                [{"employee_type_id": et.id, "quantity": eq}]
                                if et and eq > 0
                                else []
                            ),
                            "machine_types": (
                                [{"machine_type_id": mt.id, "quantity": mq}]
                                if mt and mq > 0
                                else []
                            ),
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
            formset = StagePlanningFormSet(
                initial=initial,
                company=obj.company,
                request=request,
                admin_site=self.admin_site,
            )

        context = {
            **self.admin_site.each_context(request),
            "title": _("Configure planning resources: %(name)s") % {"name": obj.name},
            "obj": obj,
            "formset": formset,
        }
        return TemplateResponse(
            request,
            "admin/configurator/producttemplate/planning.html",
            context,
        )
