from urllib.parse import urlencode

from django import forms
from django.db import models
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils import formats
from django.utils.translation import gettext, gettext_lazy as _, ngettext
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter, DropdownFilter, RangeDateFilter
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from import_export import fields, resources, widgets
from import_export.admin import ExportActionModelAdmin
from unfold.widgets import (
    UnfoldAdminTextareaWidget,
    UnfoldAdminSelect2Widget,
    UnfoldAdminTextInputWidget,
)

from core.admin.company_access import get_user_companies
from core.admin.company_admin import CompanyRestrictedAdmin
from configurator.commission.models import (
    AgentCommissionRule,
    CommissionRelease,
    CommissionReleaseStatus,
    CommissionSettlement,
    CommissionSettlementLine,
    CommissionSettlementStatus,
    InvoiceCommission,
    CommissionRuleTemplate,
    CommissionRuleType,
    QuoteCommission,
    format_rule_snapshot,
)
from configurator.commission.services import CommissionService, CommissionSettlementService
from sales.models import CommissionistAgent


class AgentCommissionRuleForm(forms.ModelForm):
    expression_name = forms.CharField(
        required=False,
        label=_("Rule name"),
        widget=UnfoldAdminTextInputWidget(),
        help_text=_(
            "Optional name."
            "If left blank, a name will be generated automatically."
        ),
    )

    expression = forms.CharField(
        required=False,
        label=_("Expression"),
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 4}),
        help_text=_(
            "Variables: subtotal, discounted_subtotal, total, quantity.\n"
            "Examples: subtotal * 0.05 — 5% of subtotal | "
            "200 — flat €200 | "
            "discounted_subtotal * 0.05 if discounted_subtotal > 5000 else 0 — 5% only above €5,000"
        ),
    )

    class Meta:
        model = AgentCommissionRule
        fields = "__all__"
        widgets = {
            "rule_type": UnfoldAdminSelect2Widget(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        instance = kwargs.get("instance")
        if instance and instance.pk and instance.rule_type == CommissionRuleType.CUSTOM:
            if instance.custom_rule:
                self.fields["expression_name"].initial = instance.custom_rule.name
                self.fields["expression"].initial = instance.custom_rule.expression

    def clean(self):
        cleaned_data = super().clean()
        rule_type = cleaned_data.get("rule_type")
        value = cleaned_data.get("value")
        expression = (cleaned_data.get("expression") or "").strip()

        if rule_type == CommissionRuleType.CUSTOM:
            if not expression:
                self.add_error(
                    "expression",
                    _("An expression is required when rule type is Custom."),
                )
            cleaned_data["value"] = None

        elif rule_type in (CommissionRuleType.PERCENTAGE, CommissionRuleType.FIXED):
            if value is None:
                self.add_error(
                    "value",
                    _("Value is required for Percentage and Fixed rules."),
                )
            cleaned_data["custom_rule"] = None
            cleaned_data["expression"] = ""
            cleaned_data["expression_name"] = ""

        return cleaned_data



class AgentCommissionRuleInline(TabularInline):
    model = AgentCommissionRule
    extra = 1
    tab = True
    verbose_name = _("Commission rule")
    verbose_name_plural = _("Commission rules")
    autocomplete_fields = ["custom_rule"]
    fields = ("rule_type", "value", "custom_rule", "is_active")



class QuoteCommissionInline(TabularInline):
    model = QuoteCommission
    extra = 0
    tab = True
    verbose_name = _("Commission")
    verbose_name_plural = _("Commissions")
    autocomplete_fields = ["agent"]

    readonly_fields = (
        "rule_type_snapshot",
        "rule_value_snapshot",
        "commission_amount",
        "released_amount",
        "pending_amount",
    )

    fields = (
        "agent",
        "rule_type_snapshot",
        "rule_value_snapshot",
        "commission_amount",
        "released_amount",
        "pending_amount",
    )

    @admin.display(description=_("Released"))
    def released_amount(self, obj):
        return obj.released_amount if obj.pk else "-"

    @admin.display(description=_("Pending release"))
    def pending_amount(self, obj):
        return obj.pending_amount if obj.pk else "-"

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False



@admin.register(CommissionRuleTemplate)
class CommissionRuleTemplateAdmin(ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "expression")

    fieldsets = (
        (_("Rule"), {
            "fields": (
                "name",
                "description",
                "expression",
                "is_active",
            ),
        }),
    )

    formfield_overrides = {
        models.TextField: {
            "widget": UnfoldAdminTextareaWidget(attrs={"rows": 4})
        },
    }



@admin.register(AgentCommissionRule)
class AgentCommissionRuleAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = AgentCommissionRuleForm
    change_form_template = "admin/configurator/agentcommissionrule/change_form.html"

    list_display = (
        "agent",
        "rule_type",
        "value_display",
        "is_active",
        "company",
    )
    list_filter = ("rule_type", "is_active", "company")
    search_fields = (
        "agent__name",
    )
    autocomplete_fields = ["agent", "company"]
    list_select_related = ["agent", "company"]

    actions_list = ["bulk_create_action"]

    fieldsets = (
        (_("Who gets the commission"), {
            "fields": (
                "agent",
                ("company", "is_active"),
            ),
        }),
        (_("Commission rule"), {
            "fields": (
                ("rule_type", "value"),
                "expression_name",
                "expression",
            ),
        }),
    )

    def value_display(self, obj):
        return obj.format_value()
    value_display.short_description = _("Value / Rule")

    def save_model(self, request, obj, form, change):
        rule_type = form.cleaned_data.get("rule_type")
        expression = (form.cleaned_data.get("expression") or "").strip()
        expression_name = (form.cleaned_data.get("expression_name") or "").strip()

        if rule_type == CommissionRuleType.CUSTOM and expression:
            # Check if this exact expression already exists in a DIFFERENT template
            existing = CommissionRuleTemplate.objects.filter(
                expression=expression
            ).exclude(
                pk=obj.custom_rule_id if obj.custom_rule_id else None
            ).first()

            if existing:
                self.message_user(
                    request,
                    _(
                        f"This expression already exists as template "
                        f"'{existing.name}'. "
                        f"The rule was not saved. "
                        f"Please use a different expression or select "
                        f"the existing template."
                    ),
                    level="error",
                )
                return  

            if obj.custom_rule_id:
                template = obj.custom_rule
                if not expression_name:
                    expression_name = template.name 
                template.name = expression_name
                template.expression = expression
                template.save()
            else:
                template = CommissionRuleTemplate.objects.filter(
                    expression=expression
                ).first()

                if not template:
                    # Create a new template
                    if not expression_name:
                        agent_str = str(obj.agent) if obj.agent_id else "?"
                        expression_name = f"Custom: {agent_str}"
                    template = CommissionRuleTemplate.objects.create(
                        name=expression_name,
                        expression=expression,
                        is_active=True,
                    )

            obj.custom_rule = template
            obj.value = None

        super().save_model(request, obj, form, change)

    @action(description=_("Bulk create rules"), icon="add")
    def bulk_create_action(self, request):
        return redirect(
            reverse("admin:configurator_agentcommissionrule_bulk_create")
        )

    def get_urls(self):
        from configurator.commission.views import (
            BulkCommissionWizardView,
            BulkCommissionConfirmView,
        )
        from configurator.commission.autocomplete_views import (
            CommissionistAgentAutocompleteView,
        )
        custom_urls = [
            path(
                "bulk-create/",
                self.admin_site.admin_view(
                    BulkCommissionWizardView.as_view(model_admin=self)
                ),
                name="configurator_agentcommissionrule_bulk_create",
            ),
            path(
                "bulk-confirm/",
                self.admin_site.admin_view(
                    BulkCommissionConfirmView.as_view(model_admin=self)
                ),
                name="configurator_agentcommissionrule_bulk_confirm",
            ),
            path(
                "autocomplete/agent/",
                self.admin_site.admin_view(CommissionistAgentAutocompleteView.as_view()),
                name="commission_agent_autocomplete",
            ),
        ]
        return custom_urls + super().get_urls()



RELEASE_STATUS_LABELS = {
    CommissionReleaseStatus.NONE: "info",
    CommissionReleaseStatus.PARTIAL: "warning",
    CommissionReleaseStatus.FULL: "success",
}


class CommissionReleaseInline(TabularInline):
    model = CommissionRelease
    extra = 0
    tab = True
    verbose_name = _("Release")
    verbose_name_plural = _("Releases")
    fields = ("release_date", "sales_invoice", "allocation", "base_amount", "released_amount")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(QuoteCommission)
class QuoteCommissionAdmin(ModelAdmin):
    inlines = [CommissionReleaseInline]
    list_display = (
        "quote",
        "agent",
        "rule_type_snapshot",
        "rule_value_display",
        "commission_amount",
        "released_amount_display",
        "pending_amount_display",
        "release_status_display",
        "calculated_at",
    )
    list_filter = ("rule_type_snapshot",)
    search_fields = (
        "agent__name",
    )
    autocomplete_fields = ["agent"]
    readonly_fields = (
        "commission_amount",
        "rule",
        "rule_type_snapshot",
        "rule_value_snapshot",
        "calculated_at",
    )
    list_select_related = ["agent", "quote", "rule"]

    def get_queryset(self, request):
        return super().get_queryset(request).with_released()

    def rule_value_display(self, obj):
        return format_rule_snapshot(obj.rule_type_snapshot, obj.rule_value_snapshot)
    rule_value_display.short_description = _("Value")

    @admin.display(description=_("Released"))
    def released_amount_display(self, obj):
        return obj.released_amount

    @admin.display(description=_("Pending release"))
    def pending_amount_display(self, obj):
        return obj.pending_amount

    @display(description=_("Release"), label=RELEASE_STATUS_LABELS)
    def release_status_display(self, obj):
        status = obj.release_status
        return status, CommissionReleaseStatus(status).label

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        CommissionService.recalculate(obj.quote)


def commission_document_label(invoice):
    """'Factura 26FC-559', 'Abono 26AB-19', 'Anticipo …' for a commission row."""
    from invoices.models import SalesInvoiceDocumentType

    if invoice.total_amount.amount < 0:
        kind = _("Credit note")
    elif invoice.document_type == SalesInvoiceDocumentType.ADVANCE:
        kind = _("Advance")
    else:
        kind = _("Invoice")
    return f"{kind} {invoice.invoice_number or invoice.proforma_number or ''}".strip()


class InvoiceCommissionResource(resources.ModelResource):
    release_date = fields.Field(
        attribute="release_date", column_name=_("Release date"), widget=widgets.DateWidget("%d/%m/%Y"),
    )
    document = fields.Field(column_name=_("Document"))
    invoice_date = fields.Field(
        attribute="sales_invoice__date_issued", column_name=_("Invoice date"), widget=widgets.DateWidget("%d/%m/%Y"),
    )
    client = fields.Field(attribute="sales_invoice__client__name", column_name=_("Client"))
    base = fields.Field(column_name=_("Base"))
    percentage = fields.Field(column_name=_("% Comm."))
    commission = fields.Field(column_name=_("Commission"))
    agent = fields.Field(attribute="agent__name", column_name=_("Agent"))

    class Meta:
        model = InvoiceCommission
        fields = ("release_date", "document", "invoice_date", "client", "base", "percentage", "commission", "agent")
        export_order = ("release_date", "document", "invoice_date", "client", "base", "percentage", "commission", "agent")

    def dehydrate_document(self, obj):
        return commission_document_label(obj.sales_invoice)

    def dehydrate_base(self, obj):
        return obj.released_base_amount.amount

    def dehydrate_percentage(self, obj):
        return obj.commission_percentage

    def dehydrate_commission(self, obj):
        return obj.released_amount.amount


class InvoiceCommissionReleaseInline(CommissionReleaseInline):
    fields = ("release_date", "allocation", "quote_commission", "base_amount", "released_amount")
    readonly_fields = fields


class ConfirmedActionMixin:
    """
    Detail actions that change data: a GET shows a confirmation page and
    its POST runs the action, so following a link never changes anything.
    """

    confirm_action_template = "admin/configurator/commission/confirm_action.html"

    def confirm_action(self, request, obj, *, title, explanation, run):
        """The confirmation page of ``obj``'s action; on its POST, ``run()`` and back to ``obj``."""
        change_url = reverse(f"admin:{self.opts.app_label}_{self.opts.model_name}_change", args=[obj.pk])
        if request.method != "POST":
            return TemplateResponse(request, self.confirm_action_template, {
                **self.admin_site.each_context(request),
                "title": title,
                "opts": self.opts,
                "object": obj,
                "explanation": explanation,
                "change_url": change_url,
            })
        run()
        return redirect(change_url)


class SettlementPendingFilter(DropdownFilter):
    """Commissions by whether confirmed settlements have paid what they released."""

    title = _("Settlement")
    parameter_name = "settlement"
    PENDING = "pending"
    SETTLED = "settled"

    def lookups(self, request, model_admin):
        return ((self.PENDING, _("Pending settlement")), (self.SETTLED, _("Fully settled")))

    def queryset(self, request, queryset):
        # The admin's queryset carries InvoiceCommissionQuerySet.with_settled()
        if self.value() == self.PENDING:
            return queryset.exclude(pending_settlement_total=0)
        if self.value() == self.SETTLED:
            return queryset.filter(pending_settlement_total=0).exclude(settled_total=0)
        return queryset


@admin.register(InvoiceCommission)
class InvoiceCommissionAdmin(ConfirmedActionMixin, CompanyRestrictedAdmin, ModelAdmin, ExportActionModelAdmin):
    """
    The commissions list: one row per invoice and agent, filtered by agent
    and date. Commissions are marked as settled here, one or a selection at
    a time, or for an agent up to a date from the agent's page.
    """

    company_field_name = "sales_invoice__company"
    resource_classes = [InvoiceCommissionResource]
    export_form_class = SelectableFieldsExportForm
    inlines = [InvoiceCommissionReleaseInline]
    list_display = (
        "release_date",
        "document_display",
        "invoice_date_display",
        "client_display",
        "released_base_amount",
        "percentage_display",
        "released_amount",
        "commission_amount",
        "release_status_display",
        "pending_settlement_display",
        "agent",
    )
    list_filter = (
        ("agent", AutocompleteSelectMultipleFilter),
        ("release_date", RangeDateFilter),
        ("sales_invoice__date_issued", RangeDateFilter),
        SettlementPendingFilter,
    )
    list_filter_submit = True
    search_fields = (
        "agent__name",
        "sales_invoice__invoice_number",
        "sales_invoice__client__name",
    )
    list_select_related = ["agent", "sales_invoice__client"]
    actions = ["mark_selected_as_settled"]
    actions_detail = ["mark_settled_detail_action", "mark_unsettled_detail_action"]
    fields = (
        ("agent", "sales_invoice", "invoice_date_display"),
        ("base_amount", "commission_amount"),
        ("released_base_amount", "released_amount", "release_date"),
        ("settled_amount_display", "pending_settlement_display"),
        ("own_base_amount", "own_commission_amount"),
        ("rule", "rule_type_snapshot", "rule_value_snapshot"),
    )
    readonly_fields = tuple(name for group in fields for name in group)

    def get_queryset(self, request):
        return super().get_queryset(request).with_settled()

    @admin.display(description=_("Settled commission"), ordering="settled_total")
    def settled_amount_display(self, obj):
        return obj.settled_amount

    @admin.display(description=_("Pending settlement"), ordering="pending_settlement_total")
    def pending_settlement_display(self, obj):
        return obj.pending_settlement_amount

    @admin.display(description=_("Document"), ordering="sales_invoice__invoice_number")
    def document_display(self, obj):
        return commission_document_label(obj.sales_invoice)

    @admin.display(description=_("Invoice date"), ordering="sales_invoice__date_issued")
    def invoice_date_display(self, obj):
        return obj.sales_invoice.date_issued

    @admin.display(description=_("Client"), ordering="sales_invoice__client__name")
    def client_display(self, obj):
        return obj.sales_invoice.client.name

    @admin.display(description=_("% Comm."))
    def percentage_display(self, obj):
        return f"{obj.commission_percentage.normalize():f}"

    @display(description=_("Release"), label=RELEASE_STATUS_LABELS)
    def release_status_display(self, obj):
        status = obj.release_status
        return status, CommissionReleaseStatus(status).label

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_mark_settled_permission(self, request, object_id=None):
        return request.user.has_perm("configurator.add_commissionsettlement")

    def has_mark_unsettled_permission(self, request, object_id=None):
        return request.user.has_perm("configurator.change_commissionsettlement")

    def _settle(self, request, commissions):
        """Settle what ``commissions`` have pending and say how many were settled."""
        settlements = CommissionSettlementService.settle_commissions(commissions, request.user)
        count = CommissionSettlementLine.objects.filter(settlement__in=settlements).count()
        if not count:
            self.message_user(request, gettext("Nothing of these commissions is pending settlement."), messages.WARNING)
            return
        self.message_user(request, ngettext(
            "%(count)d commission settled.", "%(count)d commissions settled.", count,
        ) % {"count": count}, messages.SUCCESS)

    @admin.action(description=_("Mark selected commissions as settled"), permissions=["mark_settled"])
    def mark_selected_as_settled(self, request, queryset):
        self._settle(request, queryset)

    @action(description=_("Mark as settled"), icon="task_alt", permissions=["mark_settled"])
    def mark_settled_detail_action(self, request, object_id):
        commission = get_object_or_404(self.get_queryset(request), pk=object_id)
        return self.confirm_action(
            request, commission,
            title=gettext("Mark as settled"),
            explanation=gettext("What this commission has pending is settled today and no longer shows as pending."),
            run=lambda: self._settle(request, self.get_queryset(request).filter(pk=commission.pk)),
        )

    @action(description=_("Mark as not settled"), icon="undo", permissions=["mark_unsettled"])
    def mark_unsettled_detail_action(self, request, object_id):
        commission = get_object_or_404(self.get_queryset(request), pk=object_id)

        def unsettle():
            if CommissionSettlementLine.objects.active().for_commission(commission).cancel(request.user):
                self.message_user(request, gettext("The commission is pending settlement again."), messages.SUCCESS)
            else:
                self.message_user(request, gettext("Nothing of this commission is settled."), messages.WARNING)

        return self.confirm_action(
            request, commission,
            title=gettext("Mark as not settled"),
            explanation=gettext("What was settled of this commission is pending settlement again."),
            run=unsettle,
        )


class CommissionSettlementLineInline(TabularInline):
    model = CommissionSettlementLine
    extra = 0
    tab = True
    fields = ("sales_invoice", "released_on", "base_amount", "commission_rate", "commission_amount", "cancelled_at")
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("sales_invoice")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class CommissionSettlementForm(forms.ModelForm):
    class Meta:
        model = CommissionSettlement
        fields = ("agent", "settled_until", "notes")

    def clean(self):
        cleaned_data = super().clean()
        agent, until = cleaned_data.get("agent"), cleaned_data.get("settled_until")
        if (
            self.instance.pk is None and agent and until
            and not CommissionSettlementService.pending_invoices(agent, until)
        ):
            raise CommissionSettlementService.nothing_pending(agent, until)
        return cleaned_data


@admin.register(CommissionSettlement)
class CommissionSettlementAdmin(ConfirmedActionMixin, CompanyRestrictedAdmin, ModelAdmin):
    """
    Agents' settlements: each settles what is pending up to its date, or
    the commissions chosen in the commissions list. Cancelled, never deleted.
    """

    form = CommissionSettlementForm
    inlines = [CommissionSettlementLineInline]
    list_display = (
        "id",
        "agent",
        "settled_until",
        "base_total_display",
        "commission_total_display",
        "status_display",
        "created_by",
        "created_at",
    )
    list_filter = (
        ("agent", AutocompleteSelectMultipleFilter),
        "status",
        ("settled_until", RangeDateFilter),
    )
    list_filter_submit = True
    search_fields = ("agent__name",)
    autocomplete_fields = ["agent"]
    list_select_related = ["agent", "created_by"]
    actions_detail = ["print_detail_action", "cancel_detail_action"]
    add_fields = ("agent", "settled_until", "notes")
    change_fields = (
        ("agent", "settled_until", "status_display"),
        ("base_total_display", "commission_total_display"),
        ("created_by", "created_at"),
        ("cancelled_by", "cancelled_at"),
        "notes",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).with_totals()

    def get_fields(self, request, obj=None):
        return self.change_fields if obj else self.add_fields

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return tuple(
            name for group in self.change_fields
            for name in (group if isinstance(group, tuple) else (group,))
            if name != "notes"
        )

    def get_inlines(self, request, obj):
        return self.inlines if obj else []

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "agent":
            kwargs["queryset"] = CommissionistAgent.objects.filter(company__in=get_user_companies(request.user))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
        else:
            obj.created_by = request.user
            CommissionSettlementService.settle(obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def has_cancel_permission(self, request, object_id=None):
        return request.user.has_perm("configurator.change_commissionsettlement")

    @admin.display(description=_("Base"), ordering="base_total")
    def base_total_display(self, obj):
        return formats.number_format(obj.base_total, 2, force_grouping=True)

    @admin.display(description=_("Commission"), ordering="commission_total")
    def commission_total_display(self, obj):
        return formats.number_format(obj.commission_total, 2, force_grouping=True)

    @display(
        description=_("Status"),
        ordering="status",
        label={CommissionSettlementStatus.CONFIRMED: "success", CommissionSettlementStatus.CANCELLED: "danger"},
    )
    def status_display(self, obj):
        return obj.status, obj.get_status_display()

    @action(description=_("Print"), icon="picture_as_pdf", permissions=["view"])
    def print_detail_action(self, request, object_id):
        settlement = get_object_or_404(self.get_queryset(request), pk=object_id)
        url = reverse("admin:sales_commissionistagent_reports", args=[settlement.agent_id])
        return redirect(f"{url}?{urlencode({'settlement': settlement.pk})}")

    @action(description=_("Cancel settlement"), icon="block", permissions=["cancel"])
    def cancel_detail_action(self, request, object_id):
        settlement = get_object_or_404(self.get_queryset(request), pk=object_id)

        def cancel():
            try:
                settlement.cancel(request.user)
            except ValidationError as error:
                self.message_user(request, " ".join(error.messages), level=messages.WARNING)
            else:
                self.message_user(request, gettext(
                    "Settlement %(settlement)s cancelled: its commission is pending settlement again."
                ) % {"settlement": settlement})

        return self.confirm_action(
            request, settlement,
            title=gettext("Cancel settlement"),
            explanation=gettext(
                "Its commission will be pending settlement again, and the next settlement "
                "of this agent will include it."
            ),
            run=cancel,
        )
