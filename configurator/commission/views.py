from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from formtools.wizard.views import SessionWizardView
from unfold.views import UnfoldModelAdminViewMixin
from django.views import View

from configurator.commission.forms import BulkCommissionStep1Form, BulkCommissionStep2Form
from configurator.commission.models import AgentCommissionRule, CommissionRuleTemplate, CommissionRuleType


def _resolve_custom_template(expression: str, expression_name: str) -> CommissionRuleTemplate:
    """
    Finds an existing CommissionRuleTemplate with the same expression,
    or creates a new one. Used by both the wizard and single-rule save.
    """
    expression = expression.strip()
    template = CommissionRuleTemplate.objects.filter(expression=expression).first()
    if not template:
        name = expression_name.strip() if expression_name else None
        if not name:
            from django.utils.timezone import now
            name = f"Bulk rule — {now().strftime('%Y-%m-%d %H:%M')}"
        template = CommissionRuleTemplate.objects.create(
            name=name,
            expression=expression,
            is_active=True,
        )
    return template


class BulkCommissionWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    form_list = [
        ("selection", BulkCommissionStep1Form),
        ("rule", BulkCommissionStep2Form),
    ]
    template_name = "configurator/commission/wizard_form.html"
    permission_required = ("configurator.add_agentcommissionrule",)
    title = _("Bulk Create Commission Rules")

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        kwargs["request"] = self.request
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.steps.current == "selection":
            from configurator.commission.forms import BulkCommissionStep1FormHelper
            context["helper"] = BulkCommissionStep1FormHelper()
        elif self.steps.current == "rule":
            from configurator.commission.forms import BulkCommissionStep2FormHelper
            context["helper"] = BulkCommissionStep2FormHelper()
        return context

    def done(self, form_list, **kwargs):
        form_list = list(form_list)
        step1 = form_list[0]
        step2 = form_list[1]

        agents = step1.cleaned_data["agents"]
        product_templates = step1.cleaned_data["product_templates"]
        rule_type = step2.cleaned_data["rule_type"]
        value = step2.cleaned_data.get("value")
        expression = (step2.cleaned_data.get("expression") or "").strip()
        expression_name = (step2.cleaned_data.get("expression_name") or "").strip()

        # Resolve custom template once for all combinations
        custom_rule = None
        if rule_type == CommissionRuleType.CUSTOM and expression:
            custom_rule = _resolve_custom_template(expression, expression_name)

        to_create = []
        conflicts = []

        for agent in agents:
            for product_template in product_templates:
                exists = AgentCommissionRule.objects.filter(
                    agent=agent,
                    product_template=product_template,
                ).exists()

                if exists:
                    conflicts.append({
                        "agent": str(agent),
                        "product_template": str(product_template),
                    })
                else:
                    to_create.append({
                        "agent": agent,
                        "product_template": product_template,
                        "rule_type": rule_type,
                        "value": value,
                        "custom_rule": custom_rule,
                    })

        # Store in session for the confirmation view
        self.request.session["bulk_commission_to_create"] = [
            {
                "agent_id": item["agent"].pk,
                "agent_str": str(item["agent"]),
                "product_template_id": item["product_template"].pk,
                "product_template_str": str(item["product_template"]),
                "rule_type": item["rule_type"],
                "value": str(item["value"]) if item["value"] is not None else None,
                "custom_rule_id": item["custom_rule"].pk if item["custom_rule"] else None,
                "custom_rule_str": str(item["custom_rule"]) if item["custom_rule"] else None,
            }
            for item in to_create
        ]
        self.request.session["bulk_commission_conflicts"] = conflicts

        return redirect(
            reverse("admin:configurator_agentcommissionrule_bulk_confirm")
        )


class BulkCommissionConfirmView(UnfoldModelAdminViewMixin, View):
    permission_required = ("configurator.add_agentcommissionrule",)
    title = _("Confirm Bulk Commission Rules")

    def get(self, request, *args, **kwargs):
        from django.shortcuts import render
        to_create = request.session.get("bulk_commission_to_create", [])
        conflicts = request.session.get("bulk_commission_conflicts", [])

        return render(
            request,
            "configurator/commission/confirm.html",
            {
                **self.model_admin.admin_site.each_context(request),
                "title": self.title,
                "to_create": to_create,
                "conflicts": conflicts,
                "has_conflicts": bool(conflicts),
                "has_to_create": bool(to_create),
            },
        )

    def post(self, request, *args, **kwargs):
        from decimal import Decimal
        from sales.models import CommissionistAgent
        from configurator.models import ProductTemplate

        to_create = request.session.pop("bulk_commission_to_create", [])
        request.session.pop("bulk_commission_conflicts", [])

        created_count = 0
        company = None

        for item in to_create:
            try:
                agent = CommissionistAgent.objects.get(pk=item["agent_id"])
                product_template = ProductTemplate.objects.get(pk=item["product_template_id"])
                custom_rule = None
                if item.get("custom_rule_id"):
                    custom_rule = CommissionRuleTemplate.objects.get(pk=item["custom_rule_id"])

                if company is None:
                    company = product_template.company

                AgentCommissionRule.objects.create(
                    agent=agent,
                    product_template=product_template,
                    rule_type=item["rule_type"],
                    value=Decimal(item["value"]) if item["value"] else None,
                    custom_rule=custom_rule,
                    is_active=True,
                    company=company,
                )
                created_count += 1
            except Exception:
                continue

        from django.contrib import messages
        messages.success(
            request,
            _(f"{created_count} commission rule(s) created successfully."),
        )

        return redirect(
            reverse("admin:configurator_agentcommissionrule_changelist")
        )