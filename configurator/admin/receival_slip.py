from django.contrib import admin
from unfold.admin import ModelAdmin, StackedInline
from core.admin.company_admin import CompanyRestrictedAdmin
from configurator.models import ReceivalSlip, ReceivalSlipItem
from django.utils.translation import gettext_lazy as _
from django.urls import path, reverse
from django.shortcuts import redirect
from unfold.decorators import action
from django.http import HttpRequest
from configurator.views.receival_slip_export_view import ReceivalSlipExportView

class ReceivalSlipItemInline(StackedInline):
    model = ReceivalSlipItem
    extra = 0
    verbose_name = _("Item")
    verbose_name_plural = _("Items")
    fields = (
        ("description", "quantity"),
        "measurements",
        "notes",
    )

@admin.register(ReceivalSlip)
class ReceivalSlipAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("reference", "configuration", "date", "company")
    list_filter = ("company", "date")
    search_fields = ("reference", "configuration__name", "notes")
    autocomplete_fields = ["configuration"]
    inlines = [ReceivalSlipItemInline]
    readonly_fields = ("reference", "company")
    
    actions_row = ["admin_export_receival_slip"]

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:slip_id>/export/",
                self.admin_site.admin_view(ReceivalSlipExportView.as_view(model_admin=self)),
                name="configurator_receivalslip_export",
            ),
        ]
        return custom_urls + urls

    @action(
        description=_("Export Receival Slip"),
        url_path="receivalslip-export-action",
        attrs={"target": "_blank"}
    )
    def admin_export_receival_slip(self, request: HttpRequest, object_id: int):
        url = reverse("admin:configurator_receivalslip_export", args=[object_id])
        return redirect(url)
