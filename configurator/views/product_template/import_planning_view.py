from django.template.response import TemplateResponse
from django.http import HttpRequest, HttpResponse
from django.views import View
from unfold.views import UnfoldModelAdminViewMixin
from django.contrib import messages
from django.utils.translation import gettext_lazy as _

from configurator.resources.planning_export import import_planning_xlsx
from core.admin.company_access import get_user_companies


class ImportPlanningView(UnfoldModelAdminViewMixin, View):
    title = _("Import planning resources")
    permission_required = "configuration.configure_stage_resources"
    template_name = "admin/configurator/producttemplate/import_planning.html"

    def _render(self, request, extra_context=None):
        context = {
            **self.model_admin.admin_site.each_context(request),
            "title": self.title,
            **(extra_context or {}),
        }
        return TemplateResponse(request, self.template_name, context)

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        return self._render(request)

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        uploaded = request.FILES.get("file")
        if not uploaded:
            messages.error(request, _("Please select a file."))
            return self._render(request)

        if not uploaded.name.endswith(".xlsx"):
            messages.error(request, _("Only .xlsx files are supported."))
            return self._render(request)

        # For non-superusers, restrict to their company
        company = None
        if not request.user.is_superuser:
            companies = get_user_companies(request.user)
            company = companies.first()

        try:
            file_bytes = uploaded.read()
            result = import_planning_xlsx(file_bytes, company=company)
        except Exception as e:
            messages.error(request, _("Error processing file: %(error)s") % {"error": str(e)})
            return self._render(request)

        extra = {}
        if result["errors"]:
            extra["errors"] = result["errors"]
        if result["updated"]:
            extra["updated"] = result["updated"]

        if result["updated"] and not result["errors"]:
            messages.success(
                request,
                _("Successfully updated %(count)d product(s).")
                % {"count": len(result["updated"])},
            )

        return self._render(request, extra_context=extra)
