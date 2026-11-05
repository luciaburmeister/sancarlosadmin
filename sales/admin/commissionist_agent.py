from urllib.parse import urlencode

from django.contrib import admin
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action

from core.admin.company_admin import CompanyRestrictedAdmin
from sales.models import CommissionistAgent
from sales.views.agent_report_views import AgentReportExportView


@admin.register(CommissionistAgent)
class CommissionistAgentAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "tax_identifier", "commission_percentage", "release_policy", "is_active", "company"]
    list_filter = ["is_active", "company"]
    search_fields = ["name", "tax_identifier", "email"]
    autocomplete_fields = ["company"]
    actions_row = ["export_agent_reports_action", "settle_commissions_action"]
    actions_detail = ["export_agent_reports_detail_action", "settle_commissions_detail_action"]
    fieldsets = (
        (_("Agent Information"), {
            "fields": (
                ("name", "company"),
                ("tax_identifier", "commission_percentage"),
                ("email", "phone"),
                ("release_policy", "is_active"),
            ),
        }),
    )

    def get_urls(self):
        return [
            path(
                "<int:agent_id>/reports/",
                self.admin_site.admin_view(AgentReportExportView.as_view(model_admin=self)),
                name="sales_commissionistagent_reports",
            ),
        ] + super().get_urls()

    def has_settle_commissions_permission(self, request, object_id=None):
        return request.user.has_perm("configurator.add_commissionsettlement")

    @staticmethod
    def settle_commissions_url(object_id):
        """The new settlement form, for this agent."""
        return f"{reverse('admin:configurator_commissionsettlement_add')}?{urlencode({'agent': object_id})}"

    @action(description=_("Reports"), icon="picture_as_pdf")
    def export_agent_reports_action(self, request, object_id: int):
        return redirect(reverse("admin:sales_commissionistagent_reports", args=[object_id]))

    @action(description=_("Reports"), icon="picture_as_pdf")
    def export_agent_reports_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:sales_commissionistagent_reports", args=[object_id]))

    @action(description=_("Settle commissions"), icon="request_quote", permissions=["settle_commissions"])
    def settle_commissions_action(self, request, object_id: int):
        return redirect(self.settle_commissions_url(object_id))

    @action(description=_("Settle commissions"), icon="request_quote", permissions=["settle_commissions"])
    def settle_commissions_detail_action(self, request, object_id: int):
        return redirect(self.settle_commissions_url(object_id))
