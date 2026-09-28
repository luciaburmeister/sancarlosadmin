from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View

from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from inventory.models import InventoryItem
from inventory.services.reorder_report import reorder_report_rows, reorder_report_xlsx


class InventoryReportsView(UnfoldModelAdminViewMixin, View):
    """Inventory exports. Add a button here for each report that is requested."""

    title = _("Reports")
    permission_required = ("inventory.view_inventoryitem",)
    template_name = "admin/inventory/reports.html"

    def get(self, request, **kwargs):
        request.current_app = self.model_admin.admin_site.name
        context = {
            **self.model_admin.admin_site.each_context(request),
            "title": self.title,
            "opts": InventoryItem._meta,
            "has_permission": True,
            "reports": [
                {
                    "title": _("What to order"),
                    "description": _(
                        "Accepted quotes not yet delivered, set against stock on hand "
                        "and quantity already coming in on approved purchase orders."
                    ),
                    "url": reverse("admin:inventory_inventoryitem_reorder_report"),
                },
            ],
        }
        return TemplateResponse(request, self.template_name, context)


class ReorderReportExportView(UnfoldModelAdminViewMixin, View):
    """Download the what-to-order workbook for the companies in view."""

    title = _("What to order")
    permission_required = ("inventory.view_inventoryitem",)

    def get(self, request, **kwargs):
        include_company = get_user_companies(request.user).count() > 1
        rows = reorder_report_rows(self.model_admin.get_queryset(request))
        response = HttpResponse(
            reorder_report_xlsx(rows, include_company=include_company),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        response["Content-Disposition"] = 'attachment; filename="que_pedir.xlsx"'
        return response
